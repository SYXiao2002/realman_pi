#include "realman_bt/save_camera_frames_node.hpp"

#include <cmath>
#include <cstdint>
#include <sstream>
#include <stdexcept>
#include <utility>

#include "realman_bt/policy_task_blackboard.hpp"
#include "realman_bt/runtime_snapshot.hpp"

namespace realman_bt {
namespace {

// The client keeps waiting this long after the server's own capture budget so a
// slow-but-progressing service is not abandoned before it can reply.
constexpr double kClientGraceSec = 5.0;

rclcpp::Node* getNode(const bt_core::Blackboard::Ptr& blackboard) {
  const auto value = blackboard->get<rclcpp::Node*>(kRosNodeBlackboardKey);
  if (!value.has_value() || value.value() == nullptr) {
    throw std::runtime_error("behavior tree ROS node handle is missing from blackboard");
  }
  return value.value();
}

RuntimeDiagnostics* getDiagnostics(const bt_core::Blackboard::Ptr& blackboard) {
  const auto value =
      blackboard->get<RuntimeDiagnostics*>(kRuntimeDiagnosticsBlackboardKey);
  return value.value_or(nullptr);
}

std::uint64_t timestampMilliseconds() {
  const auto now = std::chrono::system_clock::now();
  const auto milliseconds =
      std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch());
  return static_cast<std::uint64_t>(milliseconds.count());
}

}  // namespace

bt_core::PortsList SaveCameraFramesNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<std::string>(
          "image_topics", "",
          "Comma-separated camera image topics to capture (for example "
          "/camera_left/color/image_raw,/camera_right/color/image_raw)"),
      bt_core::InputPort<std::string>(
          "directory", "", "Absolute folder the JPEG snapshots are written into"),
      bt_core::InputPort<std::string>(
          "service_name", "/camera_snapshot/save", "SaveCameraImages service to call"),
      bt_core::InputPort<double>(
          "timeout_sec", "10.0", "Per-frame capture budget the service honors"),
      bt_core::InputPort<bool>(
          "dry_run", "true", "Validate the ports without capturing any frame"),
      bt_core::InputPort<bool>(
          "allow_failure", "false",
          "Tolerate a runtime capture failure (service unavailable, no frame, "
          "timeout, or refusal): record an ERROR diagnostic and return SUCCESS so "
          "a best-effort snapshot never aborts the task around it. Invalid ports "
          "still fail regardless. Defaults to false."),
      bt_core::InputPort<std::string>(
          "session_id", "",
          "Optional session tag forwarded to the snapshot service, which names "
          "each frame <label>_<session_id>.jpg so captures from one grasp cycle "
          "group together. Bind it to a blackboard key such as {grasp_session}. "
          "Empty keeps the server's capture-timestamp naming."));
}

void SaveCameraFramesNode::readPorts() {
  image_topics_.clear();
  std::stringstream stream(getInput<std::string>("image_topics").value_or(""));
  std::string item;
  while (std::getline(stream, item, ',')) {
    // Tolerate surrounding whitespace but reject an empty or relative topic.
    const auto begin = item.find_first_not_of(" \t");
    const auto end = item.find_last_not_of(" \t");
    if (begin == std::string::npos) {
      throw std::invalid_argument("image_topics has an empty entry");
    }
    const std::string topic = item.substr(begin, end - begin + 1);
    if (topic.front() != '/') {
      throw std::invalid_argument("image_topics entry must start with '/': " + topic);
    }
    for (const auto& existing : image_topics_) {
      if (existing == topic) {
        throw std::invalid_argument("image_topics has a duplicate entry: " + topic);
      }
    }
    image_topics_.push_back(topic);
  }
  if (image_topics_.empty()) {
    throw std::invalid_argument("image_topics must list at least one camera topic");
  }

  directory_ = getInput<std::string>("directory").value_or("");
  if (directory_.empty()) {
    throw std::invalid_argument("directory must be set");
  }
  if (directory_.front() != '/') {
    throw std::invalid_argument("directory must be an absolute path: " + directory_);
  }

  timeout_sec_ = getInput<double>("timeout_sec").value_or(10.0);
  if (!std::isfinite(timeout_sec_) || timeout_sec_ <= 0.0) {
    throw std::invalid_argument("timeout_sec must be positive");
  }

  service_name_ = getInput<std::string>("service_name").value_or("");
  if (service_name_.empty() || service_name_.front() != '/') {
    throw std::invalid_argument("service_name must be an absolute service name");
  }

  allow_failure_ = getInput<bool>("allow_failure").value_or(false);

  // Optional; the server sanitizes it and falls back to a capture timestamp when
  // empty, so there is nothing to validate here.
  session_id_ = getInput<std::string>("session_id").value_or("");
}

bool SaveCameraFramesNode::initialize() {
  if (initialized_) return true;
  ros_node_ = getNode(blackboard());
  readPorts();
  dry_run_ = getInput<bool>("dry_run").value_or(true);
  initialized_ = true;
  started_at_ = Clock::now();
  if (dry_run_) return true;
  client_ = ros_node_->create_client<Service>(service_name_);
  return true;
}

void SaveCameraFramesNode::recordServiceEvent(const std::string& phase,
                                              const std::string& detail,
                                              const std::string& severity) const {
  RuntimeDiagnostics* diagnostics = getDiagnostics(blackboard());
  if (!diagnostics) return;
  diagnostics->recordEvent({timestampMilliseconds(), severity, "SERVICE", service_name_,
                            phase, detail});
}

bt_core::NodeStatus SaveCameraFramesNode::failRuntime(const std::string& phase,
                                                       const std::string& reason) {
  recordServiceEvent(phase, reason, "ERROR");
  if (allow_failure_) {
    // Best-effort capture: keep the reason in the diagnostics but let the task
    // continue, so a missed frame never forces the surrounding sequence to retry.
    recordServiceEvent(phase,
                       "allow_failure set; continuing without snapshot: " + reason);
    if (ros_node_) {
      RCLCPP_WARN(ros_node_->get_logger(),
                  "[%s] snapshot failed but allow_failure is set; continuing: %s",
                  name().c_str(), reason.c_str());
    }
    completed_ = true;
    return bt_core::NodeStatus::SUCCESS;
  }
  setFailureReason(reason);
  if (ros_node_) {
    RCLCPP_ERROR(ros_node_->get_logger(), "[%s] %s", name().c_str(), reason.c_str());
  }
  failed_ = true;
  return bt_core::NodeStatus::FAILURE;
}

bt_core::NodeStatus SaveCameraFramesNode::tick() {
  try {
    if (!initialize()) return bt_core::NodeStatus::FAILURE;
  } catch (const std::exception& error) {
    setFailureReason(error.what());
    recordServiceEvent("invalid", failureReason(), "ERROR");
    if (ros_node_) {
      RCLCPP_ERROR(ros_node_->get_logger(), "[%s] invalid SaveCameraFrames request: %s",
                   name().c_str(), error.what());
    }
    failed_ = true;
    return bt_core::NodeStatus::FAILURE;
  }

  if (completed_) return bt_core::NodeStatus::SUCCESS;
  if (failed_) return bt_core::NodeStatus::FAILURE;

  if (dry_run_) {
    if (!dry_run_logged_) {
      std::ostringstream topics;
      for (std::size_t i = 0; i < image_topics_.size(); ++i) {
        if (i) topics << ", ";
        topics << image_topics_[i];
      }
      if (ros_node_) {
        RCLCPP_INFO(ros_node_->get_logger(),
                    "[%s] dry-run validated SaveCameraFrames into %s; no frame captured",
                    name().c_str(), directory_.c_str());
      }
      recordServiceEvent("result",
                         "dry-run validation completed for " + topics.str() + " into " +
                             directory_ + "; no frame captured");
      dry_run_logged_ = true;
    }
    completed_ = true;
    return bt_core::NodeStatus::SUCCESS;
  }

  if (call_ && call_->done) {
    if (call_->success) {
      recordServiceEvent("result", call_->message.empty() ? "snapshot saved"
                                                          : call_->message);
      if (ros_node_) {
        RCLCPP_INFO(ros_node_->get_logger(), "[%s] saved %zu camera frame(s) into %s",
                    name().c_str(), call_->saved_paths.size(), directory_.c_str());
      }
      completed_ = true;
      return bt_core::NodeStatus::SUCCESS;
    }
    return failRuntime("result",
                       call_->message.empty()
                           ? "camera snapshot service refused the request"
                           : call_->message);
  }

  const double elapsed = std::chrono::duration<double>(Clock::now() - started_at_).count();
  const double client_deadline = timeout_sec_ + kClientGraceSec;
  if (elapsed > client_deadline) {
    return failRuntime("timeout",
                       "camera snapshot service did not reply within " +
                           std::to_string(client_deadline) + " seconds");
  }

  if (!sent_) {
    if (!client_->service_is_ready()) {
      if (!wait_service_recorded_) {
        recordServiceEvent("wait_service",
                           "waiting for camera snapshot service " + service_name_);
        wait_service_recorded_ = true;
      }
      return bt_core::NodeStatus::RUNNING;
    }
    auto request = std::make_shared<Service::Request>();
    request->image_topics = image_topics_;
    request->directory = directory_;
    request->timeout_sec = timeout_sec_;
    request->session_id = session_id_;
    auto call = std::make_shared<SaveCall>();
    try {
      client_->async_send_request(
          request, [call](rclcpp::Client<Service>::SharedFuture future) {
            const auto response = future.get();
            call->success = response->success;
            call->message = response->message;
            call->saved_paths.assign(response->saved_paths.begin(),
                                     response->saved_paths.end());
            call->done = true;
          });
    } catch (const std::exception& error) {
      return failRuntime("send_request",
                         std::string("cannot send snapshot request: ") + error.what());
    }
    call_ = std::move(call);
    sent_ = true;
    recordServiceEvent("send_request",
                       "asked " + service_name_ + " to save " +
                           std::to_string(image_topics_.size()) + " frame(s) into " +
                           directory_);
    if (ros_node_) {
      RCLCPP_INFO(ros_node_->get_logger(), "[%s] requested camera snapshot into %s",
                  name().c_str(), directory_.c_str());
    }
  }
  return bt_core::NodeStatus::RUNNING;
}

void SaveCameraFramesNode::reset() {
  call_.reset();  // a late reply to an abandoned request must not leak into the next run
  client_.reset();
  ros_node_ = nullptr;
  image_topics_.clear();
  directory_.clear();
  session_id_.clear();
  service_name_.clear();
  timeout_sec_ = 10.0;
  initialized_ = false;
  dry_run_ = true;
  allow_failure_ = false;
  sent_ = false;
  completed_ = false;
  failed_ = false;
  dry_run_logged_ = false;
  wait_service_recorded_ = false;
  setFailureReason("");
}

void SaveCameraFramesNode::onHalted() { reset(); }

}  // namespace realman_bt
