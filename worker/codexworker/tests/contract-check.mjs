import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const schema = JSON.parse(await readFile(new URL("../../../references/hermes-manager-protocol.schema.json", import.meta.url), "utf8"));
// Dependency-free checker for the keywords actually used in this locked draft.
// This is a contract test helper, not a general JSON Schema implementation.
export function checkContract(name, value) {
  assert.ok(schema.$defs[name], `Unknown contract definition: ${name}`);
  assert.equal(valid(schema.$defs[name], value), true, `${name} does not match the locked manager schema`);
}

function valid(rule, value) {
  if (rule.$ref) return valid(rule.$ref.split("/").slice(1).reduce((object, key) => object[key], schema), value);
  if (rule.oneOf && rule.oneOf.filter(branch => valid(branch, value)).length !== 1) return false;
  if (Object.hasOwn(rule, "const") && value !== rule.const) return false;
  if (rule.enum && !rule.enum.includes(value)) return false;
  if (rule.type) {
    const types = Array.isArray(rule.type) ? rule.type : [rule.type];
    const actual = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    if (!types.includes(actual) && !(types.includes("integer") && Number.isSafeInteger(value))) return false;
  }
  if (typeof value === "string") {
    if (rule.minLength != null && [...value].length < rule.minLength) return false;
    if (rule.pattern && !new RegExp(rule.pattern).test(value)) return false;
    if (rule.format === "uuid" && !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value)) return false;
    if (rule.format === "date-time" && (!/^\d{4}-\d{2}-\d{2}T/.test(value) || !Number.isFinite(Date.parse(value)))) return false;
  }
  if (typeof value === "number" && rule.minimum != null && value < rule.minimum) return false;
  if (Array.isArray(value) && rule.items && !value.every(item => valid(rule.items, item))) return false;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    if (rule.required?.some(key => !Object.hasOwn(value, key))) return false;
    for (const [key, child] of Object.entries(rule.properties ?? {})) if (Object.hasOwn(value, key) && !valid(child, value[key])) return false;
    if (rule.additionalProperties === false && Object.keys(value).some(key => !Object.hasOwn(rule.properties ?? {}, key))) return false;
  }
  return true;
}
