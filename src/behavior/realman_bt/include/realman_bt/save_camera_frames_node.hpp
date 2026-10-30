#pragma once

#include <chrono>
#include <memory>
#include <string>
#include <vector>

#include "bt_core/leaf_node.hpp"
#include "rclcpp/rclcpp.hpp"
#include "realman_msgs/srv/save_camera_images.hpp"

namespace realman_bt {

class RuntimeDiagnostics;

// Saves the current frame of one or more cameras into a folder.
//
// The leaf itself never touches image data or the filesystem: the behavior-tree
// executor is a lean C++ process without OpenCV/cv_bridge. It delegates to a
// Python snapshot service (realman_msgs/SaveCameraImages) that subscribes to the
// requested topics, JPEG-encodes one fresh frame each, and writes them to the
// directory. image_topics carries the full camera topics (for example
// /camera_left/color/image_raw); directory is the absolute destination folder.
//
// The node sends the request once and stays RUNNING until the service replies or
// the client deadline (timeout_sec plus a grace period) elapses. SUCCESS means
// every requested frame was written; a refused request, an unavailable service,
// or a timeout is FAILURE with a preserved failureReason(). Setting allow_failure
// tolerates those runtime capture failures instead: the failure is recorded as an
// ERROR diagnostic and the node returns SUCCESS, so a best-effort snapshot never
// aborts (or forces a retry of) the task around it. Invalid ports still fail
// regardless of allow_failure, so a misconfigured tree is caught in dry-run.
//
// dry_run=true validates the port configuration and records the intent, but
// creates no service client and captures nothing.
class SaveCameraFramesNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  using Service = realman_msgs::srv::SaveCameraImages;

  static bt_core::PortsList providedPorts();

  bt_core::NodeStatus tick() override;
  void onHalted() override;

 private:
  using Clock = std::chrono::steady_clock;

  struct SaveCall {
    bool done{false};
    bool success{false};
    std::string message;
    std::vector<std::string> saved_paths;
  };

  bool initialize();
  void readPorts();
  void reset();
  void recordServiceEvent(const std::string& phase, const std::string& detail,
                          const std::string& severity = "INFO") const;
  // Records a runtime capture failure and either fails the node or, when
  // allow_failure is set, tolerates it and returns SUCCESS. Port-validation
  // errors never route through here, so they stay fatal.
  bt_core::NodeStatus failRuntime(const std::string& phase,
                                  const std::string& reason);

  rclcpp::Node* ros_node_{nullptr};
  rclcpp::Client<Service>::SharedPtr client_;
  std::string service_name_;
  std::vector<std::string> image_topics_;
  std::string directory_;
  std::string session_id_;
  double timeout_sec_{10.0};

  Clock::time_point started_at_{};
  std::shared_ptr<SaveCall> call_;
  bool initialized_{false};
  bool dry_run_{true};
  bool allow_failure_{false};
  bool sent_{false};
  bool completed_{false};
  bool failed_{false};
  bool dry_run_logged_{false};
  bool wait_service_recorded_{false};
};

}  // namespace realman_bt
