import { requestPermissionReview } from "../interface/permission-client.mjs";

const denied = () => ({ hookSpecificOutput: { hookEventName: "PermissionRequest",
  decision: { behavior: "deny", message: "The router permission review could not finish." } } });
let result;
try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 1024 * 1024) throw new Error("permission_input_too_large");
  }
  result = await requestPermissionReview(JSON.parse(input));
} catch { result = denied(); }
process.stdout.write(JSON.stringify(result) + "\n");
