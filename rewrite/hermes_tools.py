"""Eight manager tools from 02A. Only the configured host session can use them."""
from contextvars import ContextVar
from functools import partial
from hashlib import sha256
from pathlib import Path
from urllib.error import URLError

from .hermes import HermesAdapter
from .messages import valid_guid
from .service import private_config
from .storage import Fault, encode, require
from .wire import Client


TEXT = {"type": "string", "minLength": 1}
GUID = {**TEXT, "format": "uuid"}
NUMBER = {"type": "integer", "minimum": 1}
BOOL = {"type": "boolean"}
PAGE = {"cursor": {"type": "integer", "minimum": 0}, "limit": {**NUMBER, "maximum": 200}}


def fields(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


TOOLS = {
    "router_workers": ("查工位及其忙闲和持单状态", fields({**PAGE,"online":BOOL,"worker_id":TEXT})),
    "router_tasks": ("给工单号查详情，或列工单；指定 legacy_source_id 可查迁移时的旧工单", fields({
        **PAGE,"task_id":GUID,"legacy_source_id":GUID,"legacy_task_id":TEXT,
        "assignee_id":TEXT,"state":TEXT,"pool":{"type":"string","enum":["blocked"]},"unassigned":BOOL})),
    "router_create_task": ("建立正式工单，可指定处理人", fields({"title":TEXT,"instructions":TEXT,
        "assignee_id":TEXT}, ("title","instructions"))),
    "router_update_task": ("按当前版本修改工单标题或要求", fields({"task_id":GUID,"expected_revision":NUMBER,
        "changes":fields({"title":TEXT,"instructions":TEXT})}, ("task_id","expected_revision","changes"))),
    "router_act_on_task": ("按当前版本处置工单", fields({"task_id":GUID,"expected_revision":NUMBER,
        "action":{"type":"string","enum":["assign","resume","transfer","snooze","cancel","close","archive_failed"]},
        "worker_id":TEXT,"reason":TEXT,"resolution":TEXT,"note":TEXT,"remind_after_hours":{"type":"number"}},
        ("task_id","expected_revision","action"))),
    "router_info": ("查询消息；同时给出 message_id 和 attachment_id 时，校验并取得该附件在宿主侧的文件", fields({**PAGE,"message_id":GUID,"attachment_id":GUID,"source":TEXT,
        "level":{"type":"string","enum":["Event","Notify","Info"]},"since":TEXT})),
    "router_questions": ("按原单或原题查收问题和答复", fields({**PAGE,"task_id":GUID,"question_id":GUID,"pending":BOOL})),
    "router_answer": ("向该工单的指定问题交回答案", fields({"task_id":GUID,"question_id":GUID,
        "answer":fields({"mode":{"type":"string","enum":["choice","text","skip"]},"option_id":TEXT,"text":TEXT},("mode",))},
        ("task_id","question_id","answer"))),
}


class ManagerTools:
    def __init__(self, adapter, session):
        if not isinstance(session, str) or not session.strip():
            raise ValueError("a dedicated Hermes session is required")
        self.adapter, self.session = adapter, session
        self.call_context = ContextVar("mowulink_manager_call", default=None)

    def around_call(self, tool_name, next_call, args, session_id="", turn_id="", tool_call_id="", **_):
        if tool_name not in TOOLS:
            return next_call(args)
        marker = self.call_context.set((tool_name,session_id,turn_id,tool_call_id))
        try:
            return next_call(args)
        finally:
            self.call_context.reset(marker)

    def invoke(self, name, args, **_):
        try:
            origin = self.call_context.get()
            require(origin and origin[0] == name and origin[1] == self.session, 403, "wrong_host_session")
            schema = TOOLS[name][1]
            require(isinstance(args,dict) and set(schema["required"]) <= args.keys() <= schema["properties"].keys(),
                    422, "invalid_tool_arguments")
            for field in ("task_id", "question_id", "message_id", "attachment_id", "legacy_source_id"):
                if field in args:
                    require(valid_guid(args[field]), 422, "invalid_" + field)
            result = self.dispatch(name, dict(args), origin)
            return encode(result)
        except Fault as exc:
            return encode({"error":exc.code,"status":exc.status,"retryable":exc.retryable})
        except (URLError, TimeoutError, ConnectionError):
            return encode({"error":"router_unavailable","retryable":True})

    def dispatch(self, name, args, origin):
        readers = {"router_workers": self.adapter.workers, "router_tasks": self.adapter.tasks,
                   "router_questions": self.adapter.questions, "router_info": self.adapter.messages}
        if name in readers:
            if name == "router_tasks" and ("legacy_source_id" in args or "legacy_task_id" in args):
                require("legacy_source_id" in args and set(args)<={"legacy_source_id","legacy_task_id","cursor","limit"},
                        422,"invalid_history_filters")
                return self.adapter.history(args.pop("legacy_source_id"),**args)
            if name == "router_info" and "attachment_id" in args:
                require(set(args)=={"message_id","attachment_id"},422,"attachment_needs_exact_message")
                return self.adapter.read_file(args["message_id"], args["attachment_id"])
            if name == "router_tasks" and "task_id" in args:
                require(len(args) == 1, 422, "detail_with_list_filters")
                return self.adapter.inspect(args["task_id"])
            filters = {k: str(v).lower() if isinstance(v,bool) else v for k,v in args.items()}
            return readers[name](**filters)
        require(all(isinstance(v,str) and v.strip() for v in origin[1:]), 409, "missing_host_call_identity")
        key = "hermes-" + sha256(encode(origin[1:]).encode()).hexdigest()
        if name == "router_create_task":
            return self.adapter.create(key, **args)
        tid = args.pop("task_id")
        if name == "router_answer":
            return self.adapter.answer(key, tid, **args)
        revision = args.pop("expected_revision")
        if name == "router_update_task":
            return self.adapter.update(key, tid, revision, **args)
        return self.adapter.act(key, tid, revision, **args)

    def register(self, ctx):
        ctx.register_middleware("tool_execution", self.around_call)
        for name, (description, parameters) in TOOLS.items():
            schema = {"name":name,"description":description,"parameters":parameters}
            handle = ctx.register_tool(name=name, toolset="mowulink_manager", schema=schema,
                                       handler=partial(self.invoke, name))
            if handle is None:
                raise ValueError("manager tool registration declined: " + name)


def register(ctx):
    path = ctx.get_config("connection_file")
    if not path:
        raise ValueError("configure a private connection_file for the isolated manager")
    cfg = private_config(Path(path))
    client = Client(cfg["url"],cfg["token"],cfg.get("ca_file"))
    notices = None
    if "notice_chat_id" in cfg or "notice_feishu_file" in cfg:
        if not all(cfg.get(key) for key in ("notice_chat_id","notice_feishu_file","input_session_key")):
            raise ValueError("configure a Feishu notice destination, private Feishu credentials and the input session")
        import lark_oapi
        from .feishu_output import SDKMessages
        from .manager_notices import ManagerNotices
        feishu = private_config(Path(cfg["notice_feishu_file"]))
        lark = lark_oapi.Client.builder().app_id(feishu["app_id"]).app_secret(feishu["app_secret"]).timeout(8).build()
        notices = ManagerNotices(SDKMessages(lark),cfg["notice_chat_id"],feishu["app_id"])
    adapter = HermesAdapter(cfg["database"],client,None,notices=notices)
    ManagerTools(adapter,cfg["session_id"]).register(ctx)
    if "input_session_key" in cfg:
        from .hermes_host import start_input
        start_input(ctx,adapter,cfg)
