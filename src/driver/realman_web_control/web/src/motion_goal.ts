/** Shared console / grasp-page motion inputs and ExecuteMotion wire contract. */
export function parsePoseGoal(raw: string[]) {
  if (raw.length !== 7 || raw.some((value) => value.trim() === "")) return null;
  const values = raw.map(Number);
  if (!values.every(Number.isFinite) || Math.hypot(...values.slice(3)) < 1e-9) return null;
  return { position: values.slice(0, 3), quaternion: values.slice(3) };
}

export function parseMotionSettings(velocityValue: string, timeoutValue: string) {
  const velocity = Number(velocityValue), timeout = Number(timeoutValue);
  if (!Number.isInteger(velocity) || velocity < 1 || velocity > 100 || !Number.isFinite(timeout) || timeout <= 0) return null;
  return { velocity, timeout };
}

export function motionRequest(requestId: string, arm: string, command: number,
  reference: { type: number; name: string }, pose: ReturnType<typeof parsePoseGoal>,
  settings: { velocity: number; timeout: number }, joints = [0, 0, 0, 0, 0, 0]) {
  return { type: "execute_motion", request_id: requestId, arm, goal: {
    command, reference_type: reference.type, reference_name: reference.name,
    joint_degrees: joints, pose_position_m: pose?.position ?? [0, 0, 0],
    pose_quaternion_wxyz: pose?.quaternion ?? [1, 0, 0, 0],
    velocity_percent: settings.velocity, blend_radius_percent: 0, timeout_sec: settings.timeout,
  } };
}
