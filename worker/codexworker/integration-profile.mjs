// Channel policy is resolved before loading optional network credentials.
// Both profiles retain only Desktop's native task adapter as the chat backend.
export function operatingConfig(config) {
  const mode = config.operatingMode ?? "desktop-bridge";
  if (!["desktop-bridge", "feishu-local"].includes(mode)) throw new Error("unsupported_operating_mode");
  if (mode === "feishu-local") return { ...config, operatingMode: mode,
    hermesIngress: { enabled: false }, xiliu: { enabled: false } };
  return { ...config, operatingMode: mode };
}

export const optionalIngressFailure = error => ["EADDRNOTAVAIL", "EADDRINUSE", "ENETUNREACH", "EACCES"].includes(error?.code)
  ? error.code : null;
