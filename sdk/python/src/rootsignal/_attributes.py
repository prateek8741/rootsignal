"""Span attribute keys emitted by the SDK.

GenAI keys follow the OpenTelemetry GenAI semantic conventions as shipped in
``opentelemetry-semantic-conventions`` 0.66b1. They are copied here as plain
strings because the upstream module lives under ``_incubating`` and is not a
stable import path. Keys under ``rootsignal.*`` carry data the conventions do
not define (side-effect class, replay matching, capture policy).
"""

# --- OpenTelemetry general ---------------------------------------------------
ERROR_TYPE = "error.type"
SERVICE_NAME = "service.name"
DEPLOYMENT_ENVIRONMENT = "deployment.environment.name"

# --- OpenTelemetry GenAI -----------------------------------------------------
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_SYSTEM = "gen_ai.system"  # deprecated upstream; still read by many backends
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_INPUT_MESSAGES = "gen_ai.input.messages"
GEN_AI_OUTPUT_MESSAGES = "gen_ai.output.messages"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_AGENT_VERSION = "gen_ai.agent.version"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_DESCRIPTION = "gen_ai.tool.description"
GEN_AI_TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"
GEN_AI_TOOL_CALL_RESULT = "gen_ai.tool.call.result"
GEN_AI_RETRIEVAL_QUERY_TEXT = "gen_ai.retrieval.query.text"
GEN_AI_RETRIEVAL_DOCUMENTS = "gen_ai.retrieval.documents"
GEN_AI_DATA_SOURCE_ID = "gen_ai.data_source.id"

OP_INVOKE_AGENT = "invoke_agent"
OP_EXECUTE_TOOL = "execute_tool"
OP_CHAT = "chat"
OP_RETRIEVAL = "retrieval"

# --- Rootsignal ----------------------------------------------------------------
RS_SPAN_KIND = "rootsignal.span.kind"
RS_SDK_VERSION = "rootsignal.sdk.version"
# Resource attribute: "pattern" when the SDK redacts payloads, "off" otherwise.
RS_SDK_REDACTION = "rootsignal.sdk.redaction"
RS_PROMPT_VERSION = "rootsignal.prompt.version"
RS_USER_ID = "rootsignal.user.id"
RS_RUN_INPUT = "rootsignal.run.input"
RS_RUN_OUTPUT = "rootsignal.run.output"
RS_RUN_MAX_STEPS = "rootsignal.run.max_steps"
# "module:qualname" of a decorated agent function; lets replay call the same code.
RS_RUN_ENTRYPOINT = "rootsignal.run.entrypoint"
RS_TOOL_SIDE_EFFECT = "rootsignal.tool.side_effect"
RS_TOOL_ARGS_HASH = "rootsignal.tool.args_hash"
RS_TOOL_CALL_INDEX = "rootsignal.tool.call_index"
RS_TOOL_REPEAT_INDEX = "rootsignal.tool.repeat_index"
RS_MCP_SERVER = "rootsignal.mcp.server"
RS_LLM_CALL_INDEX = "rootsignal.llm.call_index"
RS_RETRIEVAL_TOP_K = "rootsignal.retrieval.top_k"
RS_RETRIEVAL_RESULT_COUNT = "rootsignal.retrieval.result_count"
RS_COST_USD = "rootsignal.cost.usd"
RS_TRUNCATED_FIELDS = "rootsignal.payload.truncated_fields"
RS_REDACTED = "rootsignal.payload.redacted"
RS_USER_INPUT_CAPTURED = "rootsignal.capture.user_input"

# Values of rootsignal.span.kind
KIND_AGENT = "agent"
KIND_LLM = "llm"
KIND_TOOL = "tool"
KIND_RETRIEVAL = "retrieval"
