const phases: Record<string, number> = { captured: 0, inferred: 1, rejected: 2 };
const text = (v: any) => typeof v === "string" && v.length > 0 && v.length <= 256;
const vector = (v: any, length: number) => Array.isArray(v) && v.length === length && v.every((x) => typeof x === "number" && Number.isFinite(x));
const pose = (v: any) => Array.isArray(v) && v.length === 4 && v.every((row) => vector(row, 4))
  && v[3].every((x: number, i: number) => Math.abs(x - (i === 3 ? 1 : 0)) < 1e-6);
const dataImage = (v: any) => typeof v === "string" && /^data:image\/(jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(v);

/** The same validator guards ROS snapshots and untrusted local review files. */
export function validSnapshot(value: any): boolean {
  if (!value || value.type !== "grasp_evaluation" || value.schema_version !== 1
    || value.read_only !== true || value.execution_ready !== false
    || !["observation_id", "profile_id", "profile_name", "review_request_id", "camera_frame", "base_frame", "target_label", "tool_name"].every((key) => text(value[key]))
    || !["l", "r"].includes(value.arm) || !["geometry", "contact_graspnet"].includes(value.backend)
    || !(value.phase in phases) || typeof value.capture_timestamp_ns !== "string"
    || !/^[1-9][0-9]{0,19}$/.test(value.capture_timestamp_ns)
    || !Number.isInteger(value.width) || !Number.isInteger(value.height)
    || value.width < 8 || value.height < 8 || value.width > 2048 || value.height > 2048
    || !Array.isArray(value.K) || value.K.length !== 3 || !value.K.every((row: any) => vector(row, 3))
    || value.K[0][0] <= 0 || value.K[1][1] <= 0 || !vector(value.seed_joint_degrees, 6)
    || (value.T_base_camera !== null && !pose(value.T_base_camera))
    || !value.images || !["rgb", "depth", "overlay"].every((kind) => dataImage(value.images[kind]))
    || !Array.isArray(value.readiness_problems) || !value.readiness_problems.every(text)
    || !value.gripper || !text(value.gripper.id)
    || !value.cloud || !Array.isArray(value.cloud.xyz_m) || !Array.isArray(value.cloud.rgb)
    || value.cloud.xyz_m.length > 4096 || value.cloud.rgb.length !== value.cloud.xyz_m.length
    || !value.cloud.xyz_m.every((p: any) => vector(p, 3))
    || !value.cloud.rgb.every((p: any) => vector(p, 3) && p.every((n: number) => Number.isInteger(n) && n >= 0 && n <= 255))
    || !Array.isArray(value.candidates) || value.candidates.length > 64) return false;
  const ids = new Set<string>();
  return value.candidates.every((candidate: any) => {
    if (!candidate || !text(candidate.candidate_id) || ids.has(candidate.candidate_id)
      || candidate.class_name !== value.target_label || !Number.isFinite(candidate.score)
      || typeof candidate.width_m !== "number" || !Number.isFinite(candidate.width_m) || candidate.width_m <= 0
      || !pose(candidate.T_camera_grasp) || (candidate.T_base_grasp !== null && !pose(candidate.T_base_grasp))) return false;
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
