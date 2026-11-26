"""Shared shape of public, grouped questions from a native host."""
from .questions import validate_answer, validate_options
from .storage import require, string


def content(question):
    if "questions" in question:
        return {"questions":question["questions"]}
    return {"text":question["text"],"options":question["options"]}


def validate_form(question):
    if "questions" not in question:
        string(question,"text")
        validate_options(question["options"])
        return
    items = question["questions"]
    require(isinstance(items,list) and 1<=len(items)<=6,422,"invalid_native_question")
    seen = set()
    for item in items:
        required = {"id","text","options","multiple","custom"}
        require(isinstance(item,dict) and required<=item.keys()<=required | {"header"},422,
                "invalid_native_question")
        key = string(item,"id")
        require(len(key)<=200 and key not in seen and len(string(item,"text"))<=3000,422,"invalid_native_question")
        seen.add(key)
        require(type(item["multiple"]) is bool and type(item["custom"]) is bool,422,"invalid_native_question")
        opts = item["options"]
        require(isinstance(opts,list) and len(opts)<=8 and (opts or item["custom"]),422,"invalid_native_question")
        if "header" in item:
            require(len(string(item,"header"))<=200,422,"invalid_native_question")
        selected = []
        for option in opts:
            require(isinstance(option,dict) and {"id","label"}<=option.keys()<={"id","label","description"},
                    422,"invalid_option")
            if "description" in option:
                require(len(string(option,"description"))<=500,422,"invalid_option")
            selected.append({"id":option["id"],"label":option["label"]})
        validate_options(selected)
        require(len({option["label"] for option in opts})==len(opts) and
                all(len(option["label"])<=300 for option in opts),422,"invalid_native_question")


def validate_reply(answer,question):
    if "questions" not in question:
        validate_answer(answer,question["options"])
        return
    require(isinstance(answer,dict) and set(answer)=={"mode","answers"} and answer["mode"]=="batch" and
            isinstance(answer["answers"],list) and len(answer["answers"])==len(question["questions"]),
            422,"invalid_answer")
    for field,value in zip(question["questions"],answer["answers"]):
        require(isinstance(value,dict) and set(value) in ({"id","option_ids"},{"id","option_ids","text"}) and
                value["id"]==field["id"] and isinstance(value["option_ids"],list),422,"invalid_answer")
        selected = value["option_ids"]
        require(all(isinstance(option,str) for option in selected),422,"invalid_option")
        require(selected==[option["id"] for option in field["options"] if option["id"] in selected],
                422,"invalid_option")
        require(field["multiple"] or len(selected)<=1,422,"invalid_native_selection")
        if "text" in value:
            require(field["custom"] and (field["multiple"] or not selected) and len(string(value,"text"))<=1000,
                    422,"invalid_answer")


def selected_labels(field,value):
    return [option["label"] for option in field["options"] if option["id"] in value["option_ids"]]
