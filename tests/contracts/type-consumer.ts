import type {
  MessageCommand, EditCommand, Scope, StopCommand, TokenUsage, CostCalculation,
  PanelCommand, StreamChunk
} from "../../packages/contracts/generated/contracts";

const scope: Scope = {
  workspace_id: "synthetic", persona_id: "synthetic", memory_space_id: "lab",
  memory_mode: "test", agent_id: "agent", run_id: "run", session_id: "session"
};
const message: MessageCommand = {
  request_id: "request", type: "dialogue.message", scope, target: "session",
  arguments: { text: "Вымышленный пример." }
};
const stop: StopCommand = { request_id: "stop", type: "agent.stop", scope, target: "agent" };
const command: PanelCommand = message;
const usage: TokenUsage = {
  input_tokens: null, output_tokens: null, source: "unknown", completeness: "unknown"
};
// @ts-expect-error STOP has no profile version or generic arguments.
const badStop: StopCommand = { ...stop, expected_version: 1 };
// @ts-expect-error A versioned edit must include expected_version.
const missingVersion: EditCommand = {
  request_id: "edit", type: "memory.correct", scope, target: "claim",
  arguments: { schema_id: "registered", data: {} }
};
// @ts-expect-error Decimal money is a string, not a binary float.
const badMoney: CostCalculation["amount"] = 0.1;
// @ts-expect-error Test/work is an explicit finite scope.
const badScope: Scope = { ...scope, memory_mode: "mixed" };
// @ts-expect-error Incomplete chunks cannot claim final status.
const badChunk: StreamChunk["preliminary"] = false;
void [command, usage, badStop, missingVersion, badMoney, badScope, badChunk];
