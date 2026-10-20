#include "realman_bt/grasp_nodes.hpp"

#include <cmath>
#include <future>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <utility>

#include "rclcpp_action/rclcpp_action.hpp"
#include "realman_bt/move_j_node.hpp"
#include "realman_msgs/action/execute_motion.hpp"
#include "realman_msgs/srv/task_control.hpp"
#include "realman_msgs/srv/task_gripper_command.hpp"
#include "realman_msgs/srv/confirm_grasp.hpp"

namespace realman_bt {
namespace {
using Clock = std::chrono::steady_clock;
using Status = bt_core::NodeStatus;
using TaskControl = realman_msgs::srv::TaskControl;
using Plan = realman_msgs::action::PlanGrasp;
using Motion = realman_msgs::action::ExecuteMotion;
using Gripper = realman_msgs::srv::TaskGripperCommand;
using Confirm = realman_msgs::srv::ConfirmGrasp;

rclcpp::Node* node(const bt_core::Blackboard::Ptr& bb) {
  auto value = bb->get<rclcpp::Node*>(kRosNodeBlackboardKey).value_or(nullptr);
  if (!value) throw std::invalid_argument("ROS node missing from blackboard");
  return value;
}
std::string token(const bt_core::Blackboard::Ptr& bb) {
  auto value = bb->get<std::string>(kTaskTokenKey).value_or("");
  if (value.empty()) throw std::invalid_argument("task lease is required");
  return value;
}
void handoff(const bt_core::Blackboard::Ptr& bb, std::shared_ptr<GraspDrain> drain) {
  if (!drain || drain->drainOnce()) return;
  auto sink = bb->get<GraspDrainSink>(kGraspDrainSinkKey);
  if (!sink.has_value()) throw std::runtime_error("grasp drain sink missing");
  sink.value()(std::move(drain));
}
void validateSink(const bt_core::Blackboard::Ptr& bb) {
  if (!bb->get<GraspDrainSink>(kGraspDrainSinkKey).has_value()) {
    throw std::invalid_argument("executor must retain grasp cancellation ownership");
  }
}
void positive(double value, const char* name) {
  if (!std::isfinite(value) || value <= 0) throw std::invalid_argument(std::string(name) + " must be positive finite");
}
std::string arm(const std::string& value) {
  if (value != "l" && value != "r") throw std::invalid_argument("grasp arm_id must be l or r");
  return value;
}
void event(const bt_core::Blackboard::Ptr& bb, const std::string& interface_name,
           const std::string& phase, const std::string& detail, const std::string& level = "INFO") {
  const auto diagnostics = bb->get<RuntimeDiagnostics*>(kRuntimeDiagnosticsBlackboardKey).value_or(nullptr);
  if (!diagnostics) return;
  auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
  diagnostics->recordEvent({static_cast<std::uint64_t>(ms), level, "EXECUTOR", interface_name, phase, detail});
}

template<class Action>
class ActionOperation : public GraspDrain {
 public:
  using Client = rclcpp_action::Client<Action>;
  using Handle = typename Client::GoalHandle;
  using Wrapped = typename Client::WrappedResult;
  ActionOperation(rclcpp::Node* ros_node, std::string name, typename Action::Goal goal,
                  std::function<void()> before_send = {})
      : name_(std::move(name)), goal_(std::move(goal)), before_send_(std::move(before_send)) {
    client_ = rclcpp_action::create_client<Action>(ros_node->get_node_base_interface(),
        ros_node->get_node_graph_interface(), ros_node->get_node_logging_interface(),
        ros_node->get_node_waitables_interface(), name_);
  }
  bool step() {
    if (!sent_) {
      if (!client_->action_server_is_ready()) return false;
      if (before_send_) before_send_();
      goal_future_ = client_->async_send_goal(goal_);
      sent_ = true;
    }
    if (!handle_) {
      if (goal_future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
      handle_ = goal_future_.get();
      if (!handle_) { terminal_ = true; throw std::runtime_error(name_ + " goal rejected"); }
    }
    if (!result_future_.valid()) result_future_ = client_->async_get_result(handle_);
    if (result_future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
    result_ = result_future_.get();
    terminal_ = true;
    if (result_.code != rclcpp_action::ResultCode::SUCCEEDED || !result_.result || !result_.result->success) {
      throw std::runtime_error(result_.result && !result_.result->message.empty() ? result_.result->message : name_ + " did not succeed");
    }
    return true;
  }
  bool drainOnce() override {
    if (terminal_ || !sent_) return true;
    try {
      if (!handle_) {
        if (goal_future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
        handle_ = goal_future_.get();
        if (!handle_) return true;
      }
      // A broken result listener must never prevent cancellation submission.
      if (result_future_.valid() && result_future_.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
        try { (void)result_future_.get(); return true; }
        catch (const std::exception&) { result_future_ = {}; }
      }
      if (!cancel_sent_) {
        (void)client_->async_cancel_goal(handle_);
        cancel_sent_ = true;
      }
      if (!result_future_.valid()) result_future_ = client_->async_get_result(handle_);
    } catch (const std::exception&) {
      // Retry listener/cancel submission; never drop an accepted goal here.
    }
    return false;
  }
  std::shared_ptr<typename Action::Result> result() const { return result_.result; }
 private:
  std::string name_;
  typename Action::Goal goal_;
  std::function<void()> before_send_;
  typename Client::SharedPtr client_;
  typename Handle::SharedPtr handle_;
  std::shared_future<typename Handle::SharedPtr> goal_future_;
  std::shared_future<Wrapped> result_future_;
  Wrapped result_{};
  bool sent_{false}, cancel_sent_{false}, terminal_{false};
};

std::shared_ptr<Plan::Result> plan(const bt_core::Blackboard::Ptr& bb, rclcpp::Node* ros_node) {
  auto value = bb->get<std::shared_ptr<Plan::Result>>(kGraspPlanKey).value_or(nullptr);
  if (!value || value->plan_id.empty()) throw std::invalid_argument("no valid grasp plan");
  if (value->expires_timestamp_ns <= ros_node->now().nanoseconds()) throw std::invalid_argument("grasp plan expired; observe again");
  return value;
}
}  // namespace

class PlanGraspOperation final : public ActionOperation<Plan> { public: using ActionOperation::ActionOperation; };
class GraspMotionOperation final : public ActionOperation<Motion> { public: using ActionOperation::ActionOperation; };

class TaskLeaseSession final : public GraspDrain {
 public:
  TaskLeaseSession(rclcpp::Node* ros_node, const std::string& arm_id, double ttl, double timeout)
      : ttl_(ttl), timeout_(timeout), started_(Clock::now()), owner_("grasp-" + std::to_string(ros_node->now().nanoseconds())) {
    client_ = ros_node->create_client<TaskControl>("/" + arm_id + "/task_control");
  }
  // Acquire/renew uses one outstanding request, retained even after timeout.
  bool step() {
    if (Clock::now() - started_ > std::chrono::duration<double>(timeout_) && token_.empty()) throw std::runtime_error("task lease acquisition timed out");
    if (future_.valid()) {
      if (future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) {
        if (!token_.empty() && Clock::now() >= expires_) throw std::runtime_error("task lease renewal timed out");
        return !token_.empty();
      }
      auto response = future_.get();
      future_ = {};
      if (!response->success || (token_.empty() && response->task_token.empty())) throw std::runtime_error("task lease denied: " + response->message);
      if (token_.empty()) token_ = response->task_token;
      // TTL starts at request submission, never at receipt of a delayed reply.
      expires_ = submitted_ + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(ttl_));
      renew_at_ = submitted_ + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(ttl_ / 3));
      if (Clock::now() >= expires_) throw std::runtime_error("task lease response arrived after expiry");
    }
    if (token_.empty() || Clock::now() >= renew_at_) {
      if (!client_->service_is_ready()) {
        if (!token_.empty() && Clock::now() >= expires_) throw std::runtime_error("task lease service unavailable at expiry");
        return !token_.empty();
      }
      auto request = std::make_shared<TaskControl::Request>();
      request->operation = token_.empty() ? TaskControl::Request::ACQUIRE : TaskControl::Request::RENEW;
      request->owner_id = owner_; request->task_token = token_; request->ttl_sec = ttl_;
      submitted_ = Clock::now();
      future_ = client_->async_send_request(request).future.share();
    }
    return !token_.empty();
  }
  bool drainOnce() override {
    // Stop renewing. Consume a late ACQUIRE before attempting authoritative RELEASE.
    try {
      if (future_.valid()) {
        if (future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
        auto response = future_.get(); future_ = {};
        if (token_.empty() && response->success) token_ = response->task_token;
      }
      if (token_.empty()) return true;
      if (release_future_.valid()) {
        if (release_future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
        auto response = release_future_.get(); release_future_ = {};
        if (response->success) { token_.clear(); return true; }
        // Driver rejects release while a motion/gripper write is still busy.
      }
      if (!client_->service_is_ready()) return false;
      auto request = std::make_shared<TaskControl::Request>();
      request->operation = TaskControl::Request::RELEASE; request->task_token = token_;
      release_future_ = client_->async_send_request(request).future.share();
    } catch (const std::exception&) {
      // A transport error must not transfer ownership to another task.
      future_ = {}; release_future_ = {};
    }
    return false;
  }
  const std::string& taskToken() const { return token_; }
  const std::string& taskId() const { return owner_; }
 private:
  rclcpp::Client<TaskControl>::SharedPtr client_;
  std::shared_future<TaskControl::Response::SharedPtr> future_, release_future_;
  double ttl_, timeout_;
  Clock::time_point started_, submitted_{}, renew_at_{}, expires_{};
  std::string owner_, token_;
};

bt_core::PortsList TaskLeaseNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<std::string>("arm_id", "r", "Profile arm namespace l/r"),
      bt_core::InputPort<bool>("dry_run", "true", "Validate without acquiring control"),
      bt_core::InputPort<bool>("execution_ready", "false", "Measured profile passed execution checks"),
      bt_core::InputPort<std::string>("readiness_detail", "", "Missing physical acceptance inputs"),
      bt_core::InputPort<double>("ttl_sec", "5", "Task lease validity, seconds"),
      bt_core::InputPort<double>("timeout_sec", "10", "Acquire timeout, seconds"));
}
void TaskLeaseNode::invalidate() {
  blackboard()->remove(kTaskTokenKey); blackboard()->remove(kGraspTaskIdKey); blackboard()->remove(kGraspPlanKey);
}
void TaskLeaseNode::cleanup() {
  if (session_) { handoff(blackboard(), session_); session_.reset(); }
}
Status TaskLeaseNode::tick() {
  try {
    if (!initialized_) {
      setFailureReason("");
      invalidate();
      const auto arm_id = arm(getInput<std::string>("arm_id").value_or(""));
      const double ttl = getInput<double>("ttl_sec").value_or(0);
      const double timeout = getInput<double>("timeout_sec").value_or(0);
      positive(ttl, "ttl_sec"); positive(timeout, "timeout_sec");
      dry_run_ = getInput<bool>("dry_run").value_or(true);
      if (!child()) throw std::invalid_argument("TaskLease requires a child");
      if (!dry_run_) {
        if (!getInput<bool>("execution_ready").value_or(false)) throw std::invalid_argument("grasp execution profile not ready: " + getInput<std::string>("readiness_detail").value_or(""));
        validateSink(blackboard());
        session_ = std::make_shared<TaskLeaseSession>(node(blackboard()), arm_id, ttl, timeout);
      }
      initialized_ = true;
    }
    if (finishing_) {
      if (session_ && !session_->drainOnce()) return Status::RUNNING;
      session_.reset();
      return outcome_;
    }
    if (session_) {
      if (!session_->step()) return Status::RUNNING;
      blackboard()->set<std::string>(kTaskTokenKey, session_->taskToken());
      blackboard()->set<std::string>(kGraspTaskIdKey, session_->taskId());
    }
    auto result = child()->executeTick();
    if (result == Status::RUNNING) return result;
    if (result != Status::SUCCESS && result != Status::FAILURE) throw std::runtime_error("task child returned IDLE");
    outcome_ = result; finishing_ = true;
    if (result == Status::FAILURE) {
      setFailureReason(child()->failureReason().empty()
          ? "grasp sequence failed; inspect the stage diagnostics" : child()->failureReason());
    }
    invalidate();
    if (session_ && !session_->drainOnce()) return Status::RUNNING;
    session_.reset();
    return outcome_;
  } catch (const std::exception& error) {
    setFailureReason(error.what());
    event(blackboard(), "grasp", "failed", error.what(), "ERROR");
    invalidate();
    if (child() && child()->needsHalt()) child()->halt();
    cleanup();
    finishing_ = true; outcome_ = Status::FAILURE;
    return Status::FAILURE;
  }
}
void TaskLeaseNode::halt() {
  invalidate();
  bt_core::DecoratorNode::halt();
  cleanup();
  initialized_ = false; finishing_ = false;
  setFailureReason("");
}

bt_core::PortsList PlanGraspNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<bool>("dry_run", "true", "Validate without observation/inference"),
      bt_core::InputPort<std::string>("profile_name", "", "Selected wrist profile"),
      bt_core::InputPort<std::string>("profile_id", "", "Frozen profile identity"),
      bt_core::InputPort<std::string>("base_frame", "", "Selected arm base frame"),
      bt_core::InputPort<std::string>("tool_name", "", "Selected controller TCP"),
      bt_core::InputPort<std::string>("target_label", "", "Explicit target class"),
      bt_core::InputPort<std::string>("backend", "geometry", "geometry/contact_graspnet/mock"),
      bt_core::InputPort<double>("timeout_sec", "30", "Planning timeout, seconds"));
}
Status PlanGraspNode::tick() {
  if (completed_) return failureReason().empty() ? Status::SUCCESS : Status::FAILURE;
  try {
    if (!operation_) {
      blackboard()->remove(kGraspPlanKey);
      Plan::Goal goal;
      goal.profile_name = getInput<std::string>("profile_name").value_or("");
      goal.target_label = getInput<std::string>("target_label").value_or("");
      goal.backend = getInput<std::string>("backend").value_or("");
      if (goal.profile_name.empty() || goal.target_label.empty()) throw std::invalid_argument("profile_name and target_label must be explicit");
      if (goal.backend != "geometry" && goal.backend != "contact_graspnet" && goal.backend != "mock") throw std::invalid_argument("invalid grasp backend");
      timeout_sec_ = getInput<double>("timeout_sec").value_or(0); positive(timeout_sec_, "timeout_sec");
      for (const auto* key : {"profile_id", "base_frame", "tool_name"}) {
        if (getInput<std::string>(key).value_or("").empty()) throw std::invalid_argument(std::string(key) + " is required");
      }
      if (getInput<bool>("dry_run").value_or(true)) {
        event(blackboard(), "/grasp_bridge/plan_grasp", "dry_run", "configuration validated; no observation or inference");
        completed_ = true; return Status::SUCCESS;
      }
      validateSink(blackboard()); goal.lease_token = token(blackboard());
      goal.task_id = blackboard()->get<std::string>(kGraspTaskIdKey).value_or("");
      operation_ = std::make_shared<PlanGraspOperation>(node(blackboard()), "/grasp_bridge/plan_grasp", goal);
      started_ = Clock::now();
    }
    if (Clock::now() - started_ > std::chrono::duration<double>(timeout_sec_)) throw std::runtime_error("PlanGrasp timed out");
    if (!operation_->step()) return Status::RUNNING;
    auto result = operation_->result();
    if (result->profile_id != getInput<std::string>("profile_id").value_or("") ||
        result->base_frame != getInput<std::string>("base_frame").value_or("") ||
        result->tool_name != getInput<std::string>("tool_name").value_or("")) throw std::runtime_error("grasp plan profile/frame/tool identity mismatch");
    if (result->observation_id.empty() || result->plan_id.empty() || result->capture_timestamp_ns <= 0 ||
        result->expires_timestamp_ns <= node(blackboard())->now().nanoseconds()) throw std::runtime_error("grasp plan is missing identity or already expired");
    blackboard()->set<std::shared_ptr<Plan::Result>>(kGraspPlanKey, result);
    event(blackboard(), "/grasp_bridge/plan_grasp", "result", "plan accepted: " + result->plan_id);
    completed_ = true; operation_.reset(); return Status::SUCCESS;
  } catch (const std::exception& error) {
    setFailureReason(error.what()); blackboard()->remove(kGraspPlanKey);
    event(blackboard(), "/grasp_bridge/plan_grasp", "failed", error.what(), "ERROR");
    if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
    completed_ = true; return Status::FAILURE;
  }
}
void PlanGraspNode::onHalted() {
  blackboard()->remove(kGraspPlanKey);
  if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
  completed_ = false; setFailureReason("");
}

bt_core::PortsList GraspWaypointNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<bool>("dry_run", "true", "Validate without motion clients/goals"),
      bt_core::InputPort<std::string>("arm_id", "r", "Profile arm namespace l/r"),
      bt_core::InputPort<std::string>("waypoint", "pregrasp", "pregrasp/grasp/lift"),
      bt_core::InputPort<int>("velocity_percent", "5", "MoveL speed ratio [1,100]"),
      bt_core::InputPort<double>("timeout_sec", "30", "Motion timeout, seconds"));
}
Status GraspWaypointNode::tick() {
  if (completed_) return failureReason().empty() ? Status::SUCCESS : Status::FAILURE;
  try {
    if (!operation_) {
      const auto arm_id = arm(getInput<std::string>("arm_id").value_or(""));
      const auto waypoint = getInput<std::string>("waypoint").value_or("");
      if (waypoint != "pregrasp" && waypoint != "grasp" && waypoint != "lift") throw std::invalid_argument("unknown grasp waypoint");
      const int velocity = getInput<int>("velocity_percent").value_or(0);
      if (velocity < 1 || velocity > 100) throw std::invalid_argument("velocity_percent must be [1,100]");
      timeout_sec_ = getInput<double>("timeout_sec").value_or(0); positive(timeout_sec_, "timeout_sec");
      if (getInput<bool>("dry_run").value_or(true)) {
        event(blackboard(), "/" + arm_id + "/execute_motion", "dry_run", "validated " + waypoint + "; no motion client");
        completed_ = true; return Status::SUCCESS;
      }
      validateSink(blackboard()); auto lease_token = token(blackboard());
      auto result = plan(blackboard(), node(blackboard()));
      Motion::Goal goal;
      goal.command = Motion::Goal::MOVEL; goal.reference_type = Motion::Goal::BASE;
      goal.reference_name = "base"; goal.task_token = lease_token;
      goal.pose_position_m = waypoint == "pregrasp" ? result->pregrasp_position_m : waypoint == "grasp" ? result->grasp_position_m : result->lift_position_m;
      goal.pose_quaternion_wxyz = waypoint == "pregrasp" ? result->pregrasp_quaternion_wxyz : waypoint == "grasp" ? result->grasp_quaternion_wxyz : result->lift_quaternion_wxyz;
      double norm = 0;
      for (const auto value : goal.pose_position_m) if (!std::isfinite(value)) throw std::invalid_argument("nonfinite grasp position");
      for (const auto value : goal.pose_quaternion_wxyz) { if (!std::isfinite(value)) throw std::invalid_argument("nonfinite grasp quaternion"); norm += value * value; }
      if (std::abs(norm - 1.0) > 1e-5) throw std::invalid_argument("grasp quaternion must be normalized");
      goal.velocity_percent = static_cast<uint32_t>(velocity); goal.blend_radius_percent = 0;
      goal.connect = false; goal.timeout_sec = static_cast<float>(timeout_sec_);
      auto bb = blackboard(); auto* ros_node = node(bb);
      operation_ = std::make_shared<GraspMotionOperation>(ros_node, "/" + arm_id + "/execute_motion", goal,
          [bb, ros_node]() { (void)plan(bb, ros_node); });
      started_ = Clock::now();
    }
    if (Clock::now() - started_ > std::chrono::duration<double>(timeout_sec_)) throw std::runtime_error("grasp waypoint timed out");
    if (!operation_->step()) return Status::RUNNING;
    completed_ = true; operation_.reset(); return Status::SUCCESS;
  } catch (const std::exception& error) {
    setFailureReason(error.what());
    event(blackboard(), "grasp_waypoint", "failed", error.what(), "ERROR");
    if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
    completed_ = true; return Status::FAILURE;
  }
}
void GraspWaypointNode::onHalted() {
  if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
  completed_ = false; setFailureReason("");
}

class TaskGripperOperation final : public GraspDrain {
 public:
  TaskGripperOperation(rclcpp::Node* ros_node, const std::string& name, Gripper::Request request,
                       std::function<void()> before_send = {})
      : request_(std::make_shared<Gripper::Request>(std::move(request))), before_send_(std::move(before_send)) {
    client_ = ros_node->create_client<Gripper>("/" + name + "/task_command");
  }
  bool step() {
    if (!sent_) {
      if (!client_->service_is_ready()) return false;
      if (before_send_) before_send_();
      future_ = client_->async_send_request(request_).future.share(); sent_ = true;
    }
    if (future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
    auto response = future_.get(); done_ = true;
    if (!response->success) throw std::runtime_error("task gripper: " + response->message);
    return true;
  }
  bool drainOnce() override {
    if (done_ || !sent_) return true;
    if (future_.wait_for(std::chrono::seconds(0)) != std::future_status::ready) return false;
    try { (void)future_.get(); } catch (const std::exception&) {}
    done_ = true; return true;
  }
 private:
  rclcpp::Client<Gripper>::SharedPtr client_;
  Gripper::Request::SharedPtr request_;
  std::function<void()> before_send_;
  std::shared_future<Gripper::Response::SharedPtr> future_;
  bool sent_{false}, done_{false};
};
bt_core::PortsList TaskGripperNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<bool>("dry_run", "true", "Validate without gripper clients/commands"),
      bt_core::InputPort<std::string>("gripper", "", "Profile gripper_right/gripper_left"),
      bt_core::InputPort<std::string>("command", "", "open/close; cancel never opens"),
      bt_core::InputPort<double>("timeout_sec", "10", "Command response timeout, seconds"));
}
Status TaskGripperNode::tick() {
  if (completed_) return failureReason().empty() ? Status::SUCCESS : Status::FAILURE;
  try {
    if (!operation_) {
      const auto name = getInput<std::string>("gripper").value_or("");
      const auto command = getInput<std::string>("command").value_or("");
      if (name != "gripper_left" && name != "gripper_right") throw std::invalid_argument("invalid task gripper");
      if (command != "open" && command != "close") throw std::invalid_argument("task gripper command must be open/close");
      timeout_sec_ = getInput<double>("timeout_sec").value_or(0); positive(timeout_sec_, "timeout_sec");
      if (getInput<bool>("dry_run").value_or(true)) { completed_ = true; event(blackboard(), "/" + name + "/task_command", "dry_run", command + "; no gripper client"); return Status::SUCCESS; }
      validateSink(blackboard());
      Gripper::Request request; request.task_token = token(blackboard());
      if (command == "close") (void)plan(blackboard(), node(blackboard()));
      request.operation = command == "open" ? Gripper::Request::OPEN : Gripper::Request::CLOSE;
      auto bb = blackboard(); auto* ros_node = node(bb);
      std::function<void()> before_send;
      if (command == "close") before_send = [bb, ros_node]() { (void)plan(bb, ros_node); };
      operation_ = std::make_shared<TaskGripperOperation>(ros_node, name, request, std::move(before_send));
      started_ = Clock::now();
    }
    if (Clock::now() - started_ > std::chrono::duration<double>(timeout_sec_)) throw std::runtime_error("task gripper response timed out; command outcome may be unknown");
    if (!operation_->step()) return Status::RUNNING;
    completed_ = true; operation_.reset(); return Status::SUCCESS;
  } catch (const std::exception& error) {
    setFailureReason(error.what());
    event(blackboard(), "task_gripper", "failed", error.what(), "ERROR");
    if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
    completed_ = true; return Status::FAILURE;
  }
}
void TaskGripperNode::onHalted() {
  if (operation_) { handoff(blackboard(), operation_); operation_.reset(); }
  completed_ = false; setFailureReason("");
}

bt_core::PortsList GraspHoldNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<bool>("dry_run", "true", "Validate without waiting"),
      bt_core::InputPort<double>("duration_sec", "1", "Hold with task lease, seconds"));
}
Status GraspHoldNode::tick() {
  try {
    if (!initialized_) {
      setFailureReason("");
      duration_sec_ = getInput<double>("duration_sec").value_or(0); positive(duration_sec_, "duration_sec");
      if (getInput<bool>("dry_run").value_or(true)) return Status::SUCCESS;
      (void)token(blackboard()); started_ = Clock::now(); initialized_ = true;
    }
    if (Clock::now() - started_ < std::chrono::duration<double>(duration_sec_)) return Status::RUNNING;
    event(blackboard(), "grasp", "held_unverified", "command sequence complete; physical grasp requires visual/operator verification");
    return Status::SUCCESS;
  } catch (const std::exception& error) { setFailureReason(error.what()); return Status::FAILURE; }
}

// The service callback only records a decision; only the BT tick can advance
// to return/release. Deactivation also rejects callbacks already queued at halt.
class GraspConfirmationSession final : public std::enable_shared_from_this<GraspConfirmationSession> {
 public:
  struct Decision { std::uint8_t outcome; bool return_object; };
  void start(rclcpp::Node* ros_node, const std::string& task_id) {
    task_id_ = task_id;
    std::weak_ptr<GraspConfirmationSession> weak = shared_from_this();
    service_ = ros_node->create_service<Confirm>("~/confirm_grasp",
        [weak](Confirm::Request::SharedPtr request, Confirm::Response::SharedPtr response) {
      const auto self = weak.lock();
      if (!self) { response->success = false; response->message = "confirmation no longer active"; return; }
      std::lock_guard<std::mutex> lock(self->mutex_);
      response->success = false;
      if (!self->active_) { response->message = "confirmation no longer active"; return; }
      if (request->task_id != self->task_id_) { response->message = "task_id does not match waiting task"; return; }
      if (request->observed_outcome != Confirm::Request::VERIFIED_GRASP &&
          request->observed_outcome != Confirm::Request::FAILED_GRASP &&
          request->observed_outcome != Confirm::Request::UNVERIFIED) {
        response->message = "observed_outcome must explicitly report verified, failed, or unverified"; return;
      }
      self->decision_ = Decision{request->observed_outcome, request->return_object};
      self->active_ = false;
      response->success = true;
      response->message = request->return_object ? "observation recorded; validated return authorized" : "observation recorded; task aborted without release";
    });
  }
  std::optional<Decision> decision() {
    std::lock_guard<std::mutex> lock(mutex_);
    return decision_;
  }
  void deactivate() {
    std::lock_guard<std::mutex> lock(mutex_);
    active_ = false;
  }
 private:
  std::mutex mutex_;
  bool active_{true};
  std::string task_id_;
  std::optional<Decision> decision_;
  rclcpp::Service<Confirm>::SharedPtr service_;
};

bt_core::PortsList GraspConfirmNode::providedPorts() {
  return bt_core::makePorts(
      bt_core::InputPort<bool>("dry_run", "true", "Validate without a confirmation service or waiting"),
      bt_core::InputPort<double>("timeout_sec", "120", "Operator observation/return decision deadline, seconds"));
}
Status GraspConfirmNode::tick() {
  if (completed_) return failureReason().empty() ? Status::SUCCESS : Status::FAILURE;
  try {
    if (!session_) {
      timeout_sec_ = getInput<double>("timeout_sec").value_or(0); positive(timeout_sec_, "timeout_sec");
      if (getInput<bool>("dry_run").value_or(true)) {
        event(blackboard(), "grasp_confirmation", "dry_run", "confirmation contract validated; no confirmation service");
        completed_ = true; return Status::SUCCESS;
      }
      (void)token(blackboard());
      auto task_id = blackboard()->get<std::string>(kGraspTaskIdKey).value_or("");
      if (task_id.empty()) throw std::invalid_argument("task_id is required for operator confirmation");
      session_ = std::make_shared<GraspConfirmationSession>();
      auto* ros_node = node(blackboard());
      session_->start(ros_node, task_id);
      started_ = Clock::now();
      event(blackboard(), ros_node->get_fully_qualified_name() + std::string("/confirm_grasp"),
          "awaiting_confirmation", "task_id=" + task_id + "; record observed outcome and explicitly authorize validated return/release");
      RCLCPP_INFO(ros_node->get_logger(), "Grasp waiting for confirmation: task_id=%s", task_id.c_str());
    }
    if (Clock::now() - started_ > std::chrono::duration<double>(timeout_sec_)) throw std::runtime_error("grasp confirmation timed out; no return or gripper release");
    const auto decision = session_->decision();
    if (!decision) return Status::RUNNING;
    const std::string outcome = decision->outcome == Confirm::Request::VERIFIED_GRASP ? "verified_grasp" :
        decision->outcome == Confirm::Request::FAILED_GRASP ? "failed_grasp" : "unverified";
    event(blackboard(), "grasp_confirmation", "observed_outcome", outcome + (decision->return_object ? "; operator authorized return/release" : "; operator declined return/release"));
    session_->deactivate(); session_.reset();
    if (!decision->return_object) throw std::runtime_error("operator declined return/release; object state=" + outcome);
    completed_ = true; return Status::SUCCESS;
  } catch (const std::exception& error) {
    if (session_) { session_->deactivate(); session_.reset(); }
    setFailureReason(error.what()); completed_ = true;
    event(blackboard(), "grasp_confirmation", "failed", error.what(), "ERROR");
    return Status::FAILURE;
  }
}
void GraspConfirmNode::onHalted() {
  if (session_) { session_->deactivate(); session_.reset(); }
  completed_ = false; setFailureReason("");
}
}  // namespace realman_bt
