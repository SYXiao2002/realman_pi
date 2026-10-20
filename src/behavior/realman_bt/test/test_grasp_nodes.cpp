#ifdef NDEBUG
#undef NDEBUG
#endif
#include <algorithm>
#include <cassert>
#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include "bt_core/xml_parser.hpp"
#include "bt_nodes/control/sequence_node.hpp"
#include "realman_bt/grasp_nodes.hpp"
#include "realman_bt/move_j_node.hpp"
#include "realman_msgs/action/execute_motion.hpp"
#include "realman_msgs/srv/task_control.hpp"
#include "realman_msgs/srv/task_gripper_command.hpp"
#include "realman_msgs/srv/confirm_grasp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

using Status = bt_core::NodeStatus;
using Lease = realman_msgs::srv::TaskControl;
using Gripper = realman_msgs::srv::TaskGripperCommand;
using Confirm = realman_msgs::srv::ConfirmGrasp;
using Plan = realman_msgs::action::PlanGrasp;
using Motion = realman_msgs::action::ExecuteMotion;
using namespace std::chrono_literals;

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  auto ros_node = std::make_shared<rclcpp::Node>("grasp_nodes_test");
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(ros_node);
  std::vector<std::shared_ptr<realman_bt::GraspDrain>> drains;
  std::vector<std::string> stages;
  int acquired = 0, renewed = 0, released = 0, gripper_calls = 0, motion_calls = 0, plan_calls = 0;
  bool leased = false, busy = false, delay_plan = false, delay_acquire = false, deny_renew = false;
  std::string fail_stage;
  std::shared_ptr<rmw_request_id_t> pending_acquire_header;
  std::shared_ptr<Lease::Response> pending_acquire_response;
  std::shared_ptr<rclcpp_action::ServerGoalHandle<Plan>> pending_plan;

  rclcpp::Service<Lease>::SharedPtr lease_server;
  lease_server = ros_node->create_service<Lease>("/r/task_control",
      [&](std::shared_ptr<rmw_request_id_t> header, std::shared_ptr<Lease::Request> req) {
    auto res = std::make_shared<Lease::Response>();
    if (req->operation == Lease::Request::ACQUIRE) {
      assert(!leased); leased = true; ++acquired; res->task_token = "test-secret";
      stages.push_back("acquire");
    } else if (req->operation == Lease::Request::RENEW) {
      assert(req->task_token == "test-secret"); ++renewed;
      if (deny_renew) { res->success = false; res->message = "revoked"; lease_server->send_response(*header, *res); return; }
    } else if (req->operation == Lease::Request::RELEASE) {
      assert(req->task_token == "test-secret");
      if (busy) { res->success = false; res->message = "busy"; lease_server->send_response(*header, *res); return; }
      leased = false; ++released; stages.push_back("release");
    }
    res->success = true;
    if (delay_acquire && req->operation == Lease::Request::ACQUIRE) {
      pending_acquire_header = header; pending_acquire_response = res;
    } else lease_server->send_response(*header, *res);
  });
  auto gripper_server = ros_node->create_service<Gripper>("/gripper_right/task_command",
      [&](std::shared_ptr<Gripper::Request> req, std::shared_ptr<Gripper::Response> res) {
    assert(leased && req->task_token == "test-secret");
    ++gripper_calls;
    const bool had_close = std::find(stages.begin(), stages.end(), "close") != stages.end();
    stages.push_back(req->operation == Gripper::Request::OPEN ? (had_close ? "release_open" : "open") : "close");
    res->success = fail_stage != stages.back();
  });
  auto plan_server = rclcpp_action::create_server<Plan>(ros_node, "/grasp_bridge/plan_grasp",
      [&](const rclcpp_action::GoalUUID&, std::shared_ptr<const Plan::Goal> req) {
    assert(leased && req->lease_token == "test-secret"); assert(!req->task_id.empty());
    ++plan_calls; stages.push_back("plan"); return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }, [](auto) { return rclcpp_action::CancelResponse::ACCEPT; },
      [&](std::shared_ptr<rclcpp_action::ServerGoalHandle<Plan>> handle) {
    if (delay_plan) { pending_plan = handle; return; }
    auto result = std::make_shared<Plan::Result>();
    result->success = fail_stage != "plan"; result->plan_id = "plan"; result->observation_id = "observation";
    result->profile_id = "profile"; result->base_frame = "r/base_link"; result->tool_name = "tcpgrip";
    result->capture_timestamp_ns = ros_node->now().nanoseconds();
    result->expires_timestamp_ns = result->capture_timestamp_ns + 10000000000LL;
    result->pregrasp_position_m = {0.2, 0.1, 0.3}; result->grasp_position_m = {0.2, 0.1, 0.2}; result->lift_position_m = {0.2, 0.1, 0.4};
    result->pregrasp_quaternion_wxyz = result->grasp_quaternion_wxyz = result->lift_quaternion_wxyz = {1, 0, 0, 0};
    if (result->success) handle->succeed(result); else handle->abort(result);
  });
  auto motion_server = rclcpp_action::create_server<Motion>(ros_node, "/r/execute_motion",
      [&](const rclcpp_action::GoalUUID&, std::shared_ptr<const Motion::Goal> req) {
    assert(leased && req->task_token == "test-secret"); ++motion_calls;
    stages.push_back(req->command == Motion::Goal::MOVEJ ? (req->joint_degrees[0] == 1 ? "observe" : "return") : req->pose_position_m[2] == 0.3 ? "pregrasp" : req->pose_position_m[2] == 0.2 ? "grasp" : "lift");
    return rclcpp_action::GoalResponse::ACCEPT_AND_EXECUTE;
  }, [](auto) { return rclcpp_action::CancelResponse::ACCEPT; },
      [&](std::shared_ptr<rclcpp_action::ServerGoalHandle<Motion>> handle) {
    auto result = std::make_shared<Motion::Result>();
    result->success = fail_stage != stages.back();
    result->message = result->success ? "complete" : "simulated stage failure";
    if (result->success) handle->succeed(result); else handle->abort(result);
  });

  bt_core::NodeFactory factory;
  factory.registerNodeType<bt_nodes::SequenceNode>("Sequence");
  factory.registerNodeType<realman_bt::TaskLeaseNode>("TaskLease");
  factory.registerNodeType<realman_bt::PlanGraspNode>("PlanGrasp");
  factory.registerNodeType<realman_bt::TaskGripperNode>("TaskGripper");
  factory.registerNodeType<realman_bt::GraspWaypointNode>("GraspWaypoint");
  factory.registerNodeType<realman_bt::GraspHoldNode>("GraspHold");
  factory.registerNodeType<realman_bt::GraspConfirmNode>("GraspConfirm");
  factory.registerNodeType<realman_bt::MoveJNode>("MoveJ");
  bt_core::XmlParser parser(factory);
  auto bb = bt_core::Blackboard::create();
  realman_bt::RuntimeDiagnostics diagnostics;
  bb->set<realman_bt::RuntimeDiagnostics*>(realman_bt::kRuntimeDiagnosticsBlackboardKey, &diagnostics);
  bb->set<rclcpp::Node*>(realman_bt::kRosNodeBlackboardKey, ros_node.get());
  bb->set<realman_bt::GraspDrainSink>(realman_bt::kGraspDrainSinkKey, [&](auto value) { drains.push_back(value); });
  bb->set<realman_bt::MoveJCancellationDrainSink>(realman_bt::kMoveJCancellationDrainSinkBlackboardKey,
      [](auto) { assert(false && "no halted observation goal expected"); });
  bb->set<bool>("dry_run", true);
  bb->set<std::string>("arm_id", "r");
  for (const auto& kv : std::vector<std::pair<std::string, std::string>>{
      {"grasp_profile_name", "right_wrist"}, {"grasp_profile_id", "profile"},
      {"grasp_base_frame", "r/base_link"}, {"grasp_tool_name", "tcpgrip"},
      {"grasp_gripper", "gripper_right"}, {"grasp_target_label", "box"},
      {"grasp_backend", "mock"}, {"grasp_observation_joint_degrees", "1,2,3,4,5,6"},
      {"grasp_return_joint_degrees", "7,8,9,10,11,12"},
      {"grasp_readiness_detail", ""}}) bb->set<std::string>(kv.first, kv.second);
  bb->set<bool>("grasp_execution_ready", true);
  bb->set<int>("grasp_velocity_percent", 5);
  for (const auto* key : {"grasp_acquire_timeout_sec", "grasp_motion_timeout_sec", "grasp_plan_timeout_sec", "grasp_gripper_timeout_sec"}) bb->set<double>(key, 2.0);
  bb->set<double>("grasp_lease_ttl_sec", 0.15);
  bb->set<double>("grasp_hold_sec", 0.15);
  bb->set<double>("grasp_confirmation_timeout_sec", 2.0);
  auto tree = parser.loadFromFile(GRASP_TREE_FILE, bb);
  auto drain = [&]() {
    for (auto it = drains.begin(); it != drains.end();) {
      if ((*it)->drainOnce()) it = drains.erase(it); else ++it;
    }
  };
  auto pump = [&]() { executor.spin_some(); drain(); std::this_thread::sleep_for(2ms); };
  rclcpp::Client<Confirm>::SharedPtr confirm_client;
  std::uint8_t observed_outcome = Confirm::Request::VERIFIED_GRASP;
  bool auto_confirm = true;
  auto waiting = [&]() {
    const auto task_id = bb->get<std::string>(realman_bt::kGraspTaskIdKey).value_or("");
    if (task_id.empty()) return false;
    for (const auto& entry : diagnostics.snapshot().events) {
      if (entry.phase == "awaiting_confirmation" && entry.detail.find("task_id=" + task_id + ";") == 0) return true;
    }
    return false;
  };
  auto run = [&]() {
    bool confirmation_sent = false;
    std::shared_future<Confirm::Response::SharedPtr> confirmation_future;
    auto deadline = std::chrono::steady_clock::now() + 8s;
    while (std::chrono::steady_clock::now() < deadline) {
      pump(); auto status = tree.tickOnce();
      if (auto_confirm && !confirmation_sent && confirm_client && confirm_client->service_is_ready() &&
          waiting() && !stages.empty() && stages.back() == "lift") {
        auto req = std::make_shared<Confirm::Request>();
        req->task_id = bb->get<std::string>(realman_bt::kGraspTaskIdKey).value_or("");
        req->observed_outcome = observed_outcome;
        req->return_object = fail_stage != "confirm";
        confirmation_future = confirm_client->async_send_request(req).future.share();
        stages.push_back("confirm"); confirmation_sent = true;
      }
      if (confirmation_future.valid() && confirmation_future.wait_for(0s) == std::future_status::ready) {
        assert(confirmation_future.get()->success); confirmation_future = {};
      }
      if (status != Status::RUNNING) return status;
    }
    assert(false && "tree timeout"); return Status::FAILURE;
  };

  // Complete validation creates neither lease nor robot/gripper/planning requests.
  assert(run() == Status::SUCCESS);
  assert(acquired == 0 && motion_calls == 0 && gripper_calls == 0 && plan_calls == 0);
  assert(ros_node->get_service_names_and_types().count("/grasp_nodes_test/confirm_grasp") == 0);
  tree.halt();
  bb->set<bool>("dry_run", false);
  confirm_client = ros_node->create_client<Confirm>("/grasp_nodes_test/confirm_grasp");
  assert(run() == Status::SUCCESS);
  assert(stages == std::vector<std::string>({"acquire", "open", "observe", "plan", "pregrasp", "grasp", "close", "lift", "confirm", "return", "release_open", "release"}));
  assert(renewed > 0 && !leased);
  assert(!bb->get<std::string>(realman_bt::kTaskTokenKey).has_value());
  assert(!bb->get<std::shared_ptr<Plan::Result>>(realman_bt::kGraspPlanKey).has_value());

  // Every stage fails closed, releases once idle, and prevents later commands.
  const std::vector<std::string> task_stages{"open", "observe", "plan", "pregrasp", "grasp", "close", "lift", "confirm", "return", "release_open"};
  for (std::size_t i = 0; i < task_stages.size(); ++i) {
    tree.halt(); stages.clear(); fail_stage = task_stages[i];
    assert(run() == Status::FAILURE);
    assert(!tree.root()->failureReason().empty());
    std::vector<std::string> expected{"acquire"};
    expected.insert(expected.end(), task_stages.begin(), task_stages.begin() + i + 1);
    expected.push_back("release");
    assert(stages == expected && !leased);
  }
  fail_stage.clear();

  auto wait_for_confirmation = [&]() {
    auto deadline = std::chrono::steady_clock::now() + 4s;
    while (std::chrono::steady_clock::now() < deadline) {
      pump(); assert(tree.tickOnce() == Status::RUNNING);
      if (waiting() && confirm_client->service_is_ready()) return;
    }
    assert(false && "confirmation service not reached");
  };
  auto confirm = [&](const std::string& id, std::uint8_t outcome, bool return_object) {
    auto req = std::make_shared<Confirm::Request>();
    req->task_id = id; req->observed_outcome = outcome; req->return_object = return_object;
    auto future = confirm_client->async_send_request(req).future.share();
    auto deadline = std::chrono::steady_clock::now() + 2s;
    while (future.wait_for(0s) != std::future_status::ready && std::chrono::steady_clock::now() < deadline) pump();
    assert(future.wait_for(0s) == std::future_status::ready);
    return future.get()->success;
  };

  // Operator wait retains/renews ownership. Stale IDs and implicit/invalid
  // observations cannot authorize motion. A failed observation may explicitly
  // authorize safe return; an expired grasp plan must not prevent that return.
  tree.halt(); stages.clear(); auto_confirm = false;
  wait_for_confirmation();
  const auto first_task_id = bb->get<std::string>(realman_bt::kGraspTaskIdKey).value();
  const int renew_before_wait = renewed;
  for (int i = 0; i < 40; ++i) { pump(); assert(tree.tickOnce() == Status::RUNNING); }
  assert(leased && renewed > renew_before_wait && stages.back() == "lift");
  assert(!confirm("stale-task", Confirm::Request::VERIFIED_GRASP, true));
  assert(!confirm(first_task_id, 0, true));
  assert(tree.tickOnce() == Status::RUNNING && stages.back() == "lift");
  bb->get<std::shared_ptr<Plan::Result>>(realman_bt::kGraspPlanKey).value()->expires_timestamp_ns = 1;
  assert(confirm(first_task_id, Confirm::Request::FAILED_GRASP, true));
  assert(!confirm(first_task_id, Confirm::Request::VERIFIED_GRASP, true));  // one decision only
  stages.push_back("confirm");
  assert(run() == Status::SUCCESS && !leased);
  assert(stages[stages.size() - 3] == "return" && stages[stages.size() - 2] == "release_open");
  bool reported_failed = false;
  for (const auto& entry : diagnostics.snapshot().events) {
    assert(entry.source == "EXECUTOR" || entry.source == "ACTION");
    assert(entry.detail.find("test-secret") == std::string::npos);
    if (entry.phase == "observed_outcome" && entry.detail.find("failed_grasp;") == 0) reported_failed = true;
  }
  assert(reported_failed);

  // Unverified is an explicit observation, never silently promoted to success.
  tree.halt(); stages.clear(); auto_confirm = true; observed_outcome = Confirm::Request::UNVERIFIED;
  assert(run() == Status::SUCCESS && !leased);
  observed_outcome = Confirm::Request::VERIFIED_GRASP;

  // Timeout after lift must not run return or issue a second open command.
  tree.halt(); stages.clear(); auto_confirm = false;
  bb->set<double>("grasp_confirmation_timeout_sec", 0.04);
  assert(run() == Status::FAILURE && !leased);
  assert(stages == std::vector<std::string>({"acquire", "open", "observe", "plan", "pregrasp", "grasp", "close", "lift", "release"}));
  bb->set<double>("grasp_confirmation_timeout_sec", 2.0);

  // Halt removes the confirmation endpoint and retains normal lease cleanup.
  tree.halt(); stages.clear(); wait_for_confirmation();
  const auto halted_task_id = bb->get<std::string>(realman_bt::kGraspTaskIdKey).value();
  tree.halt();
  auto cleanup_deadline = std::chrono::steady_clock::now() + 4s;
  while (!drains.empty() && std::chrono::steady_clock::now() < cleanup_deadline) pump();
  assert(!leased && drains.empty() && stages.back() == "release");
  assert(std::find(stages.begin(), stages.end(), "return") == stages.end());
  tree.halt(); stages.clear(); wait_for_confirmation();
  assert(!confirm(halted_task_id, Confirm::Request::VERIFIED_GRASP, true));
  assert(confirm(bb->get<std::string>(realman_bt::kGraspTaskIdKey).value(), Confirm::Request::UNVERIFIED, false));
  assert(run() == Status::FAILURE && !leased);
  assert(std::find(stages.begin(), stages.end(), "return") == stages.end());
  auto_confirm = true;

  // A halt before an ACQUIRE reply retains the late grant and releases it;
  // the child never sees that lease or issues its first gripper command.
  tree.halt(); stages.clear(); delay_acquire = true;
  auto deadline = std::chrono::steady_clock::now() + 4s;
  while (!pending_acquire_response && std::chrono::steady_clock::now() < deadline) {
    pump(); assert(tree.tickOnce() == Status::RUNNING);
  }
  assert(pending_acquire_response && leased);
  tree.halt();
  assert(!bb->get<std::string>(realman_bt::kTaskTokenKey).has_value() && !drains.empty());
  lease_server->send_response(*pending_acquire_header, *pending_acquire_response);
  pending_acquire_header.reset(); pending_acquire_response.reset(); delay_acquire = false;
  deadline = std::chrono::steady_clock::now() + 4s;
  while (!drains.empty() && std::chrono::steady_clock::now() < deadline) pump();
  assert(drains.empty() && !leased && stages == std::vector<std::string>({"acquire", "release"}));

  // Halt a live planner: invalidate first, then retain its pending response until cancel is terminal.
  tree.halt(); stages.clear(); delay_plan = true;
  deadline = std::chrono::steady_clock::now() + 4s;
  while (!pending_plan && std::chrono::steady_clock::now() < deadline) { pump(); assert(tree.tickOnce() == Status::RUNNING); }
  assert(pending_plan);
  busy = true; tree.halt();
  assert(!bb->get<std::string>(realman_bt::kTaskTokenKey).has_value());
  assert(!drains.empty());
  for (int i = 0; i < 10; ++i) pump();
  assert(leased);  // The driver refuses release until the command boundary drains.
  busy = false;
  deadline = std::chrono::steady_clock::now() + 4s;
  while (!drains.empty() && std::chrono::steady_clock::now() < deadline) {
    pump();
    if (pending_plan && pending_plan->is_canceling()) {
      auto result = std::make_shared<Plan::Result>(); result->success = false;
      pending_plan->canceled(result); pending_plan.reset();
    }
  }
  assert(drains.empty() && !leased);
  assert(stages == std::vector<std::string>({"acquire", "open", "observe", "plan", "release"}));

  // Renewal denial stops a running plan and retains cancellation cleanup.
  tree.halt(); stages.clear();
  deadline = std::chrono::steady_clock::now() + 4s;
  while (!pending_plan && std::chrono::steady_clock::now() < deadline) { pump(); assert(tree.tickOnce() == Status::RUNNING); }
  assert(pending_plan); deny_renew = true;
  assert(run() == Status::FAILURE);
  assert(!bb->get<std::string>(realman_bt::kTaskTokenKey).has_value());
  deadline = std::chrono::steady_clock::now() + 4s;
  while (!drains.empty() && std::chrono::steady_clock::now() < deadline) {
    pump();
    if (pending_plan && pending_plan->is_canceling()) {
      auto result = std::make_shared<Plan::Result>(); result->success = false;
      pending_plan->canceled(result); pending_plan.reset();
    }
  }
  assert(drains.empty() && !leased);
  assert(stages == std::vector<std::string>({"acquire", "open", "observe", "plan", "release"}));

  // Rejected physical readiness cannot acquire a lease or open the gripper.
  tree.halt(); bb->set<bool>("grasp_execution_ready", false); stages.clear();
  assert(run() == Status::FAILURE && stages.empty());

  // Readiness can arrive after a leaf was constructed. Expiry must be checked
  // again at async_send_goal/request, rather than only before client creation.
  tree.halt();
  bb->set<std::string>(realman_bt::kTaskTokenKey, "test-secret");
  auto delayed_plan = std::make_shared<Plan::Result>();
  delayed_plan->plan_id = "delayed-plan";
  delayed_plan->expires_timestamp_ns = ros_node->now().nanoseconds() + 10000000000LL;
  delayed_plan->pregrasp_position_m = {0.2, 0.1, 0.3};
  delayed_plan->pregrasp_quaternion_wxyz = {1, 0, 0, 0};
  bb->set<std::shared_ptr<Plan::Result>>(realman_bt::kGraspPlanKey, delayed_plan);
  auto delayed_motion = parser.loadFromText(
      "<root main_tree_to_execute='M'><BehaviorTree ID='M'><GraspWaypoint arm_id='l' waypoint='pregrasp' velocity_percent='5' dry_run='false' timeout_sec='2'/></BehaviorTree></root>", bb);
  auto delayed_close = parser.loadFromText(
      "<root main_tree_to_execute='G'><BehaviorTree ID='G'><TaskGripper gripper='gripper_left' command='close' dry_run='false' timeout_sec='2'/></BehaviorTree></root>", bb);
  assert(delayed_motion.tickOnce() == Status::RUNNING);
  assert(delayed_close.tickOnce() == Status::RUNNING);
  delayed_plan->expires_timestamp_ns = 1;
  int delayed_motion_submissions = 0, delayed_close_submissions = 0;
  auto delayed_motion_server = rclcpp_action::create_server<Motion>(ros_node, "/l/execute_motion",
      [&](const rclcpp_action::GoalUUID&, std::shared_ptr<const Motion::Goal>) {
        ++delayed_motion_submissions; return rclcpp_action::GoalResponse::REJECT;
      }, [](auto) { return rclcpp_action::CancelResponse::ACCEPT; }, [](auto) {});
  auto delayed_close_server = ros_node->create_service<Gripper>("/gripper_left/task_command",
      [&](Gripper::Request::SharedPtr, Gripper::Response::SharedPtr res) { ++delayed_close_submissions; res->success = false; });
  auto delayed_motion_status = Status::RUNNING, delayed_close_status = Status::RUNNING;
  deadline = std::chrono::steady_clock::now() + 3s;
  while ((delayed_motion_status == Status::RUNNING || delayed_close_status == Status::RUNNING) &&
         std::chrono::steady_clock::now() < deadline) {
    pump();
    if (delayed_motion_status == Status::RUNNING) delayed_motion_status = delayed_motion.tickOnce();
    if (delayed_close_status == Status::RUNNING) delayed_close_status = delayed_close.tickOnce();
  }
  assert(delayed_motion_status == Status::FAILURE && delayed_close_status == Status::FAILURE);
  assert(delayed_motion.root()->failureReason().find("expired") != std::string::npos);
  assert(delayed_close.root()->failureReason().find("expired") != std::string::npos);
  for (int i = 0; i < 5; ++i) pump();
  assert(delayed_motion_submissions == 0 && delayed_close_submissions == 0);
  rclcpp::shutdown();
  return 0;
}
