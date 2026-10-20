#pragma once

#include <chrono>
#include <functional>
#include <memory>
#include <string>

#include "bt_core/decorator_node.hpp"
#include "bt_core/leaf_node.hpp"
#include "rclcpp/rclcpp.hpp"
#include "realman_msgs/action/plan_grasp.hpp"

namespace realman_bt {

// All asynchronous work outlives its XML node when a tree is halted. A lease
// drain releases only through the driver's authoritative busy check.
class GraspDrain {
 public:
  virtual ~GraspDrain() = default;
  virtual bool drainOnce() = 0;
};
using GraspDrainSink = std::function<void(std::shared_ptr<GraspDrain>)>;
inline constexpr char kGraspDrainSinkKey[] = "__realman_bt_grasp_drain_sink__";
inline constexpr char kTaskTokenKey[] = "__realman_bt_task_token__";
inline constexpr char kGraspTaskIdKey[] = "__realman_bt_grasp_task_id__";
inline constexpr char kGraspPlanKey[] = "__realman_bt_grasp_plan__";

class TaskLeaseSession;
class TaskLeaseNode final : public bt_core::DecoratorNode {
 public:
  using bt_core::DecoratorNode::DecoratorNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void halt() override;
 private:
  void invalidate();
  void cleanup();
  std::shared_ptr<TaskLeaseSession> session_;
  bool initialized_{false};
  bool dry_run_{true};
  bool finishing_{false};
  bt_core::NodeStatus outcome_{bt_core::NodeStatus::FAILURE};
};

class PlanGraspOperation;
class PlanGraspNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void onHalted() override;
 private:
  std::shared_ptr<PlanGraspOperation> operation_;
  std::chrono::steady_clock::time_point started_{};
  double timeout_sec_{0};
  bool completed_{false};
};

class GraspMotionOperation;
class GraspWaypointNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void onHalted() override;
 private:
  std::shared_ptr<GraspMotionOperation> operation_;
  std::chrono::steady_clock::time_point started_{};
  double timeout_sec_{0};
  bool completed_{false};
};

class TaskGripperOperation;
class TaskGripperNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void onHalted() override;
 private:
  std::shared_ptr<TaskGripperOperation> operation_;
  std::chrono::steady_clock::time_point started_{};
  double timeout_sec_{0};
  bool completed_{false};
};

class GraspHoldNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void onHalted() override { initialized_ = false; setFailureReason(""); }
 private:
  std::chrono::steady_clock::time_point started_{};
  double duration_sec_{0};
  bool initialized_{false};
};

class GraspConfirmationSession;
class GraspConfirmNode final : public bt_core::ActionNode {
 public:
  using bt_core::ActionNode::ActionNode;
  static bt_core::PortsList providedPorts();
  bt_core::NodeStatus tick() override;
  void onHalted() override;
 private:
  std::shared_ptr<GraspConfirmationSession> session_;
  std::chrono::steady_clock::time_point started_{};
  double timeout_sec_{0};
  bool completed_{false};
};

}  // namespace realman_bt
