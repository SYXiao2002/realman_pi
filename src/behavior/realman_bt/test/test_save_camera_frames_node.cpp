// A Release build defines NDEBUG, which would turn every assert below into a no-op.
#ifdef NDEBUG
#undef NDEBUG
#endif

#include <cassert>
#include <chrono>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "realman_bt/move_j_node.hpp"
#include "realman_bt/runtime_snapshot.hpp"
#include "realman_bt/save_camera_frames_node.hpp"
#include "realman_msgs/srv/save_camera_images.hpp"

namespace {

using namespace std::chrono_literals;
using bt_core::NodeStatus;
using realman_bt::RuntimeDiagnostics;
using realman_bt::SaveCameraFramesNode;
using SaveCameraImages = realman_msgs::srv::SaveCameraImages;

// A stand-in for camera_snapshot_server: serves SaveCameraImages on one node. The
// leaf-under-test creates its client on this same node (via the blackboard), so a
// single executor drives both the request and the reply, exactly like the driver and
// leaf share the executor node in production.
struct FakeSnapshotServer {
  FakeSnapshotServer() {
    node = std::make_shared<rclcpp::Node>("fake_camera_snapshot");
    executor.add_node(node);
  }

  void serve() {
    service = node->create_service<SaveCameraImages>(
        "/camera_snapshot/save",
        [this](const SaveCameraImages::Request::SharedPtr request,
               SaveCameraImages::Response::SharedPtr response) {
          ++calls;
          last_topics.assign(request->image_topics.begin(), request->image_topics.end());
          last_directory = request->directory;
          last_timeout = request->timeout_sec;
          last_session = request->session_id;
          response->success = reply_success;
          response->message = reply_message;
          response->saved_paths = reply_paths;
        });
  }

  std::shared_ptr<rclcpp::Node> node;
  rclcpp::executors::SingleThreadedExecutor executor;
  rclcpp::Service<SaveCameraImages>::SharedPtr service;
  bool reply_success{true};
  std::string reply_message{"saved 2 frame(s)"};
  std::vector<std::string> reply_paths{"/tmp/snap/camera_left_x.jpg",
                                       "/tmp/snap/camera_right_x.jpg"};
  int calls{0};
  std::vector<std::string> last_topics;
  std::string last_directory;
  double last_timeout{0.0};
  std::string last_session;
};

struct Fixture {
  explicit Fixture(bool dry_run = false,
                   std::string image_topics =
                       "/camera_left/color/image_raw,/camera_right/color/image_raw",
                   std::string directory = "/tmp/snap",
                   std::string service_name = "/camera_snapshot/save",
                   std::string timeout_sec = "2.0",
                   bool allow_failure = false,
                   std::string session_id = "") {
    bt_core::NodeConfig node_config;
    node_config.blackboard = bt_core::Blackboard::create();
    blackboard = node_config.blackboard;
    blackboard->set<rclcpp::Node*>(realman_bt::kRosNodeBlackboardKey, server.node.get());
    blackboard->set<RuntimeDiagnostics*>(realman_bt::kRuntimeDiagnosticsBlackboardKey,
                                         &diagnostics);
    node_config.port_values = {
        {"image_topics", image_topics},
        {"directory", directory},
        {"service_name", service_name},
        {"timeout_sec", timeout_sec},
        {"dry_run", dry_run ? "true" : "false"},
        {"allow_failure", allow_failure ? "true" : "false"},
        {"session_id", session_id},
    };
    node = std::make_unique<SaveCameraFramesNode>("save_camera_frames", node_config);
  }

  NodeStatus run(std::chrono::milliseconds limit) {
    const auto deadline = std::chrono::steady_clock::now() + limit;
    NodeStatus status = NodeStatus::IDLE;
    while (std::chrono::steady_clock::now() < deadline) {
      server.executor.spin_some();
      status = node->executeTick();
      if (status != NodeStatus::RUNNING) return status;
      std::this_thread::sleep_for(5ms);
    }
    return status;
  }

  FakeSnapshotServer server;
  RuntimeDiagnostics diagnostics;
  bt_core::Blackboard::Ptr blackboard;
  std::unique_ptr<SaveCameraFramesNode> node;
};

void testPortsAreDeclared() {
  const auto ports = SaveCameraFramesNode::providedPorts();
  assert(ports.count("image_topics") == 1);
  assert(ports.count("directory") == 1);
  assert(ports.count("service_name") == 1);
  assert(ports.count("timeout_sec") == 1);
  assert(ports.count("dry_run") == 1);
  assert(ports.count("allow_failure") == 1);
  assert(ports.count("session_id") == 1);
}

void testDryRunValidatesWithoutCapturing() {
  Fixture f(true);
  f.server.serve();

  assert(f.run(500ms) == NodeStatus::SUCCESS);
  assert(f.server.calls == 0);
}

void testASuccessfulSnapshotSendsThePortsAndSucceeds() {
  Fixture f;
  f.server.serve();

  assert(f.run(3s) == NodeStatus::SUCCESS);

  assert(f.server.calls == 1);
  assert(f.server.last_topics.size() == 2);
  assert(f.server.last_topics[0] == "/camera_left/color/image_raw");
  assert(f.server.last_topics[1] == "/camera_right/color/image_raw");
  assert(f.server.last_directory == "/tmp/snap");
  assert(f.server.last_timeout > 0.0);
}

void testSessionIdIsForwardedToTheServer() {
  // A grasp cycle binds session_id="{grasp_session}"; the leaf must forward the
  // resolved tag so the server names this cycle's frames with it.
  Fixture f(false, "/camera_right/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "2.0", /*allow_failure=*/false, /*session_id=*/"20261029_143748");
  f.server.serve();

  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.calls == 1);
  assert(f.server.last_session == "20261029_143748");
}

void testAnEmptySessionIdIsForwardedAsEmpty() {
  // No session tag (for example snapshot.xml) forwards empty so the server falls
  // back to its own capture timestamp.
  Fixture f(false, "/camera_right/color/image_raw");
  f.server.serve();

  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.last_session.empty());
}

void testARefusedSaveIsAFailure() {
  Fixture f;
  f.server.reply_success = false;
  f.server.reply_message = "no frame within 2.0s for: /camera_right/color/image_raw";
  f.server.serve();

  assert(f.run(3s) == NodeStatus::FAILURE);

  assert(f.server.calls == 1);
  assert(f.node->failureReason().find("no frame within") != std::string::npos);
}

void testAMissingServiceIsATimeoutFailure() {
  Fixture f(false, "/camera_left/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "0.2");
  // No server.serve(): the client deadline (timeout_sec + grace) must fire.

  assert(f.run(7s) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("did not reply within") != std::string::npos);
}

void testARelativeTopicIsRejected() {
  Fixture f(false, "camera_left/color/image_raw");

  assert(f.run(500ms) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("must start with '/'") != std::string::npos);
  assert(f.server.calls == 0);
}

void testAnEmptyTopicListIsRejected() {
  Fixture f(false, "");

  assert(f.run(500ms) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("at least one camera topic") != std::string::npos);
}

void testADuplicateTopicIsRejected() {
  Fixture f(false, "/camera_left/color/image_raw,/camera_left/color/image_raw");

  assert(f.run(500ms) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("duplicate") != std::string::npos);
}

void testARelativeDirectoryIsRejected() {
  Fixture f(false, "/camera_left/color/image_raw", "relative/snap");

  assert(f.run(500ms) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("absolute path") != std::string::npos);
}

void testHaltAbandonsAnInFlightRequestAndRestarts() {
  Fixture f(false, "/camera_left/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "30.0");
  // No service yet: the node keeps waiting for it and stays RUNNING.
  assert(f.run(200ms) == NodeStatus::RUNNING);

  f.node->halt();
  f.server.serve();
  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.calls == 1);
}

void testASecondRunAfterSuccessCapturesAgain() {
  Fixture f;
  f.server.serve();
  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.calls == 1);

  // The leaf holds SUCCESS until halted (like MoveJ), so a Sequence does not
  // re-run it; a parent that restarts it halts first, which clears the state.
  f.node->halt();
  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.calls == 2);
}

void testASuccessfulSnapshotRecordsServiceDiagnostics() {
  Fixture f;
  f.server.serve();

  assert(f.run(3s) == NodeStatus::SUCCESS);

  const auto snapshot = f.diagnostics.snapshot();
  bool saw_send = false;
  bool saw_result = false;
  for (const auto& event : snapshot.events) {
    assert(event.source == "SERVICE");
    assert(event.interface_name == "/camera_snapshot/save");
    if (event.phase == "send_request") saw_send = true;
    if (event.phase == "result") saw_result = true;
  }
  assert(saw_send);
  assert(saw_result);
}

void testAllowFailureToleratesARefusedSave() {
  Fixture f(false, "/camera_left/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "2.0", /*allow_failure=*/true);
  f.server.reply_success = false;
  f.server.reply_message = "no frame within 2.0s for: /camera_left/color/image_raw";
  f.server.serve();

  // A best-effort snapshot records the refusal but never aborts the task.
  assert(f.run(3s) == NodeStatus::SUCCESS);
  assert(f.server.calls == 1);

  const auto snapshot = f.diagnostics.snapshot();
  bool saw_error = false;
  for (const auto& event : snapshot.events) {
    if (event.phase == "result" && event.severity == "ERROR") saw_error = true;
  }
  assert(saw_error);
}

void testAllowFailureToleratesAMissingService() {
  Fixture f(false, "/camera_left/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "0.2", /*allow_failure=*/true);
  // No server.serve(): the client deadline fires, but allow_failure tolerates it.
  assert(f.run(7s) == NodeStatus::SUCCESS);
}

void testAllowFailureStillRejectsInvalidPorts() {
  // A misconfigured tree must still fail so dry-run surfaces it: allow_failure
  // tolerates runtime capture failures, not invalid ports.
  Fixture f(false, "camera_left/color/image_raw", "/tmp/snap", "/camera_snapshot/save",
            "2.0", /*allow_failure=*/true);
  assert(f.run(500ms) == NodeStatus::FAILURE);
  assert(f.node->failureReason().find("must start with '/'") != std::string::npos);
  assert(f.server.calls == 0);
}

}  // namespace

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  testPortsAreDeclared();
  testDryRunValidatesWithoutCapturing();
  testASuccessfulSnapshotSendsThePortsAndSucceeds();
  testSessionIdIsForwardedToTheServer();
  testAnEmptySessionIdIsForwardedAsEmpty();
  testARefusedSaveIsAFailure();
  testAMissingServiceIsATimeoutFailure();
  testARelativeTopicIsRejected();
  testAnEmptyTopicListIsRejected();
  testADuplicateTopicIsRejected();
  testARelativeDirectoryIsRejected();
  testHaltAbandonsAnInFlightRequestAndRestarts();
  testASecondRunAfterSuccessCapturesAgain();
  testASuccessfulSnapshotRecordsServiceDiagnostics();
  testAllowFailureToleratesARefusedSave();
  testAllowFailureToleratesAMissingService();
  testAllowFailureStillRejectsInvalidPorts();
  rclcpp::shutdown();
  return 0;
}
