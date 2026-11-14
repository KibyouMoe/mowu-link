// A Desktop launched from another Worker must not inherit that engine's
// state directory or pretend to be its already-authorized business child.
export function normalizeDesktopEnvironment(env) {
  const foreignTransport = env.CODEXWORKER_NATIVE_TRANSPORT &&
    env.CODEXWORKER_NATIVE_TRANSPORT !== "desktop-pipe";
  if (foreignTransport) {
    delete env.CODEXWORKER_NATIVE_TRANSPORT;
    delete env.CODEXWORKER_STATE_DIR;
    delete env.CODEXWORKER_NATIVE_IMAGE_INPUT;
    delete env.CODEXWORKER_NATIVE_QUESTION_INPUT;
    delete env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR;
  }
  for (const name of ["RUDOLF_BRIDGE_LIFECYCLE_CHILD", "RUDOLF_BRIDGE_LIFECYCLE_READY",
    "RUDOLF_BRIDGE_DESKTOP_STARTED_AT", "RUDOLF_BRIDGE_LIFETIME_LAUNCH",
    "RUDOLF_BRIDGE_RUNTIME_HOST_PID", "CODEXWORKER_DESKTOP_PID"]) delete env[name];
  return env;
}
