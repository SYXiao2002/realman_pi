const phases: Record<string, number> = { captured: 0, inferred: 1, planned: 2, rejected: 2 };
const text = (v: any) => typeof v === "string" && v.length > 0 && v.length <= 256;
const vector = (v: any, length: number) => Array.isArray(v) && v.length === length && v.every((x) => typeof x === "number" && Number.isFinite(x));
const pose = (v: any) => Array.isArray(v) && v.length === 4 && v.every((row) => vector(row, 4))
  && v[3].every((x: number, i: number) => Math.abs(x - (i === 3 ? 1 : 0)) < 1e-6);
const dataImage = (v: any) => typeof v === "string" && /^data:image\/(jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(v);
const finiteOrNull = (v: any) => v === null || (typeof v === "number" && Number.isFinite(v));
const finiteVector = (v: any, length: number) => vector(v, length) && v.every((n: any) => typeof n === "number" && Number.isFinite(n));
const collisionStatuses = new Set(["safe", "warning", "collision", "unknown"]);
const collisionGeometry = (v: any) => {
  if (v === null) return true;
  if (!v || v.model !== "visual_finger_boxes/v1" || v.frame !== "grasp_contact"
    || typeof v.opening_m !== "number" || !Number.isFinite(v.opening_m) || v.opening_m <= 0
    || !Array.isArray(v.boxes) || v.boxes.length !== 2) return false;
  const names = new Set(["left_finger", "right_finger"]);
  if (!v.boxes.every((box: any) => names.delete(box?.name)
    && finiteVector(box.center_m, 3) && finiteVector(box.size_m, 3)
    && box.size_m.every((n: number) => n > 0))) return false;
  const left = v.boxes.find((box: any) => box.name === "left_finger"), right = v.boxes.find((box: any) => box.name === "right_finger");
  return left.size_m.every((n: number, i: number) => Math.abs(n - right.size_m[i]) < 1e-9)
    && Math.abs(left.center_m[1]) < 1e-9 && Math.abs(right.center_m[1]) < 1e-9
    && Math.abs(left.center_m[2] + left.size_m[2] / 2) < 1e-9
    && Math.abs(right.center_m[2] + right.size_m[2] / 2) < 1e-9
    && left.center_m[0] < 0 && right.center_m[0] > 0
    && Math.abs(Math.abs(left.center_m[0]) - Math.abs(right.center_m[0])) < 1e-9
    && Math.abs(v.opening_m - 2 * (Math.abs(left.center_m[0]) - left.size_m[0] / 2)) < 1e-9;
};
const collisionMetric = (value: any) => value && collisionStatuses.has(value.status)
  && finiteOrNull(value.min_clearance_m) && finiteOrNull(value.max_penetration_m)
  && (value.status === "unknown" || (value.min_clearance_m !== null && value.max_penetration_m !== null))
  && (value.max_penetration_m === null || value.max_penetration_m >= 0)
  && ["collision_points", "near_points", "local_environment_points", "collision_clusters"].every((key) =>
    Number.isInteger(value[key]) && value[key] >= 0);
function validCollision(value: any, displayedPointCount: number): boolean {
  if (!value || !collisionStatuses.has(value.status) || !collisionGeometry(value.geometry)
    || !collisionMetric(value)) return false;
  if (value.blocking !== undefined && typeof value.blocking !== "boolean") return false;
  if (value.reason !== undefined && value.reason !== null && !text(value.reason)) return false;
  if (value.status !== "unknown" && value.geometry === null) return false;
  if (value.status !== "unknown" && !["pregrasp", "approach_sweep", "grasp", "lift"].includes(value.worst_stage)) return false;
  if (value.worst_stage !== null && !["pregrasp", "approach_sweep", "grasp", "lift"].includes(value.worst_stage)) return false;
  if (!value.stages || !["pregrasp", "approach_sweep", "grasp", "lift"].every((key) => collisionMetric(value.stages[key]))) return false;
  if (value.approach_m !== undefined && value.approach_m !== null
    && (typeof value.approach_m !== "number" || !Number.isFinite(value.approach_m) || value.approach_m <= 0)) return false;
  if (value.lift_m !== undefined && value.lift_m !== null
    && (typeof value.lift_m !== "number" || !Number.isFinite(value.lift_m) || value.lift_m <= 0)) return false;
  if (value.thresholds !== undefined) {
    const thresholds = value.thresholds;
    if (typeof thresholds.enabled !== "boolean"
      || !["warning_clearance_m", "collision_penetration_m", "sweep_step_m", "cluster_voxel_m"].every((key) =>
        typeof thresholds[key] === "number" && Number.isFinite(thresholds[key]) && thresholds[key] > 0)
      || thresholds.collision_penetration_m > thresholds.warning_clearance_m
      || !Number.isInteger(thresholds.minimum_local_points) || thresholds.minimum_local_points < 1
      || !Number.isInteger(thresholds.minimum_collision_points) || thresholds.minimum_collision_points < 1
      || !Array.isArray(thresholds.blocking_statuses) || !thresholds.blocking_statuses.length
      || !thresholds.blocking_statuses.every((status: any) => collisionStatuses.has(status))) return false;
  }
  if (value.display !== undefined) {
    if (!Array.isArray(value.display.near_mask) || !Array.isArray(value.display.collision_mask)
      || value.display.near_mask.length !== displayedPointCount || value.display.collision_mask.length !== displayedPointCount
      || !value.display.near_mask.every((bit: any) => bit === 0 || bit === 1)
      || !value.display.collision_mask.every((bit: any) => bit === 0 || bit === 1)
      || value.display.near_mask.some((bit: number, index: number) => bit === 1 && value.display.collision_mask[index] === 1)) return false;
  }
  return true;
}

/** The same validator guards ROS snapshots and untrusted local review files. */
export function validSnapshot(value: any): boolean {
  if (!value || value.type !== "grasp_evaluation" || value.schema_version !== 1
    || value.read_only !== true || value.execution_ready !== false
    || !["observation_id", "profile_id", "profile_name", "review_request_id", "camera_frame", "base_frame", "target_label", "tool_name"].every((key) => text(value[key]))
    || !["l", "r"].includes(value.arm) || !["geometry", "contact_graspnet"].includes(value.backend)
    || !(value.phase in phases) || (value.source !== undefined && !["web_evaluation", "behavior_tree"].includes(value.source))
    || typeof value.capture_timestamp_ns !== "string"
    || !/^[1-9][0-9]{0,19}$/.test(value.capture_timestamp_ns)
    || !Number.isInteger(value.width) || !Number.isInteger(value.height)
    || value.width < 8 || value.height < 8 || value.width > 2048 || value.height > 2048
    || !Array.isArray(value.K) || value.K.length !== 3 || !value.K.every((row: any) => vector(row, 3))
    || value.K[0][0] <= 0 || value.K[1][1] <= 0 || !vector(value.seed_joint_degrees, 6)
    || (value.T_base_camera !== null && !pose(value.T_base_camera))
    || !value.images || (value.source !== "behavior_tree" && !["rgb", "depth", "overlay"].every((kind) => dataImage(value.images[kind])))
    || !Array.isArray(value.readiness_problems) || !value.readiness_problems.every(text)
    || !value.gripper || !text(value.gripper.id)
    || !value.cloud || !Array.isArray(value.cloud.xyz_m) || !Array.isArray(value.cloud.rgb)
    || value.cloud.xyz_m.length > 35000 || value.cloud.rgb.length !== value.cloud.xyz_m.length
    || (value.cloud.displayed_point_count !== undefined && (!Number.isInteger(value.cloud.displayed_point_count)
      || value.cloud.displayed_point_count < 0 || value.cloud.displayed_point_count !== value.cloud.xyz_m.length))
    || !value.cloud.xyz_m.every((p: any) => vector(p, 3))
    || !value.cloud.rgb.every((p: any) => vector(p, 3) && p.every((n: number) => Number.isInteger(n) && n >= 0 && n <= 255))
    || !Array.isArray(value.candidates) || value.candidates.length > 64) return false;
  if (value.workspace_m !== undefined && (!Array.isArray(value.workspace_m) || value.workspace_m.length !== 2
    || !value.workspace_m.every((v: any) => vector(v, 3))
    || value.workspace_m[0].some((v: number, i: number) => v >= value.workspace_m[1][i]))) return false;
  const ids = new Set<string>();
  return value.candidates.every((candidate: any) => {
    if (!candidate || !text(candidate.candidate_id) || ids.has(candidate.candidate_id)
      || candidate.class_name !== value.target_label || !Number.isFinite(candidate.score)
      || typeof candidate.width_m !== "number" || !Number.isFinite(candidate.width_m) || candidate.width_m <= 0
      || !pose(candidate.T_camera_grasp) || (candidate.T_base_grasp !== null && !pose(candidate.T_base_grasp))
      || (candidate.collision !== undefined && !validCollision(candidate.collision, value.cloud.displayed_point_count ?? value.cloud.xyz_m.length))) return false;
    for (const key of ["T_camera_native_grasp", "T_camera_native_grasp_aligned", "T_camera_grasp_before_alignment", "T_camera_grasp_after_alignment", "T_base_grasp_before_alignment", "T_base_grasp_after_alignment"]) {
      if (candidate[key] != null && !pose(candidate[key])) return false;
    }
    if (candidate.vertical_alignment !== undefined) {
      const alignment = candidate.vertical_alignment;
      if (!alignment || typeof alignment.applied !== "boolean" || !["applied", "missing_base_camera", "backend_not_applicable"].includes(alignment.status)
        || alignment.applied !== (alignment.status === "applied") || !pose(candidate.T_camera_grasp_before_alignment)
        || alignment.applied !== (candidate.T_camera_native_grasp_aligned != null)
        || alignment.applied !== (candidate.T_camera_grasp_after_alignment != null)) return false;
    }
    ids.add(candidate.candidate_id); return true;
  });
}

export function newerSnapshot(next: any, previous: any): boolean {
  if (!previous) return true;
  const nextTime = BigInt(next.capture_timestamp_ns), previousTime = BigInt(previous.capture_timestamp_ns);
  if (nextTime !== previousTime) return nextTime > previousTime;
  return next.observation_id === previous.observation_id && next.profile_id === previous.profile_id
    && phases[next.phase] > phases[previous.phase];
}
