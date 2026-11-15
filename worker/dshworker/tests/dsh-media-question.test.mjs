import test from "node:test";
import { runProbes, assertFixed } from "./repro-media-question.mjs";

test("DSH media is standalone, ingress is idempotent and twenty question deliveries produce one card and receipt", async () => {
  const results = await runProbes(() => {});
  assertFixed(results);
});
