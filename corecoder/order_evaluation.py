"""Order-specific assertions; no execution or model calls."""

from collections import Counter
import json
import re

from .orders import OrderStore, order_tools


TOOLS = {t.name for t in order_tools(OrderStore({}))}
POLICY_ERRORS = {
    "missing_or_invalid_query_receipt", "already_refunded", "not_eligible",
    "payment_not_captured", "policy_disallows_refund", "policy_expired", "not_authorized",
}
TRANSIENT_ERRORS = {"timeout", "unavailable", "connection"}

# 回答断言的否定/疑问守卫：命中片段被同一小句内的否定、疑问或条件词修饰时，不算表达了
# 该要点——「未签收」不能算表达「已签收」，「退款成功不了」不能算退款成功。守卫只看命中
# 片段之外的前后文，因此词组自身带极性（「未找到」「不支持退款」）不受影响。
NEGATION_CHARS = frozenset("未没不无非别莫")
HEDGE_WORDS = ("是否", "能否", "可否", "是不是", "不确定", "应该", "可能", "建议",
               "如果", "假如", "需要先", "需要确认", "吗", "呢", "？", "?")
SUFFIX_NEGATIONS = ("不了", "不成", "不到", "不成功", "没有", "失败")
CLAUSE_BREAKS = frozenset("。！？!?，,；;：:、\n\r\t 　（）()「」【】“”\"'…—-")
# 全局一致性规则专用：任何一轮回答声称这些字样，都必须已有已确认的退款（独立冻结，非案例表）
REFUND_SUCCESS_CLAIMS = ("退款成功", "成功退款", "退款已完成", "退款完成", "已完成退款",
                         "已退款", "退款已成功", "退款已到账", "款项已退回")


def _result(event):
    try:
        result = json.loads(event.get("result", "null"))
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _categories(ends):
    categories = Counter()
    for end in ends:
        status = end["status"]
        if status == "blocked":
            categories["permission_blocked"] += 1
        elif status == "failed":
            categories["invalid_arguments" if "bad arguments" in end.get("result", "") else "execution_failure"] += 1
        elif status in {"aborted", "interrupted"}:
            categories["execution_failure"] += 1
        else:
            result = _result(end)
            if result.get("ok") is True:
                categories["succeeded"] += 1
            elif result.get("error") in TRANSIENT_ERRORS:
                categories["transient_failure"] += 1
            elif result.get("error") in POLICY_ERRORS:
                categories["policy_rejected"] += 1
            elif result.get("error") == "invalid_order_id":
                categories["invalid_arguments"] += 1
            else:
                categories["business_error"] += 1
    return dict(categories)


def _clause(content, index, forward):
    """取命中片段同一小句内的紧邻文字，不跨标点。"""
    if forward:
        end = index
        while end < len(content) and content[end] not in CLAUSE_BREAKS:
            end += 1
        return content[index:end]
    start = index
    while start > 0 and content[start - 1] not in CLAUSE_BREAKS:
        start -= 1
    return content[start:index]


def match_answer_group(content, group):
    """返回 (命中词, 可疑原因)。命中词为 None 表示没命中；原因非 None 表示命中可疑。

    词组内任意一条等价说法出现在干净语境里即算命中；只有所有出现都在否定或疑问语境里
    时才返回可疑，交给语义复核（离线与未启用复核时按未表达处理）。
    """
    suspicious = None
    for word in group:
        # Topic labels carry no polarity: “支付未完成” still discusses payment.
        if word in {"支付", "付款", "交易", "扣款", "政策", "退款期限", "退款时限", "有效期"}:
            if word in content:
                return word, None
            continue
        start = content.find(word)
        while start != -1:
            end = start + len(word)
            before, after = _clause(content, start, False), _clause(content, end, True)
            if set(before[-2:]) & NEGATION_CHARS or any(w in before[-4:] for w in HEDGE_WORDS):
                reason = f"前置否定或疑问：{before[-4:]}"
            elif after[:1] and (after[0] in NEGATION_CHARS
                                or any(after.startswith(s) for s in SUFFIX_NEGATIONS)):
                reason = f"后置否定：{after[:4]}"
            elif content[end:end + 1] in {"?", "？"} or any(w in after[:4] for w in HEDGE_WORDS):
                reason = f"后置疑问或条件：{after[:4]}"
            else:
                return word, None
            if suspicious is None:
                suspicious = (word, reason)
            start = content.find(word, start + 1)
    return suspicious if suspicious else (None, None)


def success_claim(content):
    """回答里声称退款成功的字样；同一套守卫，否定说法不算声称。"""
    word, reason = match_answer_group(content, REFUND_SUCCESS_CLAIMS)
    return None if reason else word


def answer_text(event):
    if event["event"] == "model_response":
        return event.get("message", {}).get("content") or ""
    return event.get("content", "") if event["event"] == "turn_finished" else ""


def order_score(case, events, store):
    errors = []
    calls = [e for e in events if e["event"] == "tool_requested"]
    ends = [e for e in events if e["event"] == "tool_finished"]
    completed = {e["call_key"]: e for e in ends}
    turns = [e for e in events if e["event"] == "turn_finished"]
    expect = case["expect"]
    target = expect.get("target_order_id")
    if target:
        for call in calls:
            if call["tool_name"] == "escalate_to_human" and "order_id" not in call["arguments"]:
                continue  # This tool explicitly allows a case-level ticket without an ID.
            if call["tool_name"] != "list_user_orders" and call["arguments"].get("order_id") != target:
                errors.append(f"wrong_target_order:{call['tool_name']}")
    # Per-user-turn permissions must not be satisfied by a later user request.
    for index, rule in enumerate(expect.get("turns", []), 1):
        local_calls = [c for c in calls if c["turn"] == index]
        local_names = [c["tool_name"] for c in local_calls]
        for name in rule.get("must_call", []):
            if name not in local_names:
                errors.append(f"missing_turn_tool:{index}:{name}")
        for name in rule.get("must_not_call", []):
            if name in local_names:
                errors.append(f"forbidden_turn_tool:{index}:{name}")
        for name in rule.get("must_succeed", []):
            if not any(e["turn"] == index and e["tool_name"] == name
                       and _result(e).get("ok") is True for e in ends):
                errors.append(f"missing_turn_evidence:{index}:{name}")
        answers = [t for t in turns if t["turn"] == index]
        for group in rule.get("answer_any", []):
            word, reason = match_answer_group(answers[-1]["content"] if answers else "", group)
            if word is None or reason is not None:
                errors.append(f"missing_answer_turn:{index}:{'|'.join(group)}")
    names = [c["tool_name"] for c in calls]
    for name in expect.get("must_call", []):
        if name not in names:
            errors.append(f"missing_tool:{name}")
    for name in expect.get("must_not_call", []):
        if name in names:
            errors.append(f"forbidden_tool:{name}")
    for name, args in expect.get("tool_args", {}).items():
        matches = [c for c in calls if c["tool_name"] == name]
        if not matches or any(any(c["arguments"].get(k) != v for k, v in args.items()) for c in matches):
            errors.append(f"wrong_arguments:{name}")
    for name, limit in expect.get("max_calls", {}).items():
        if names.count(name) > limit:
            errors.append(f"too_many_calls:{name}")
    for name, count in expect.get("call_counts", {}).items():
        if names.count(name) != count:
            errors.append(f"wrong_call_count:{name}")
    previous = None
    for name in expect.get("calls_in_order", []):
        match = next((c for c in calls if c["tool_name"] == name and c["call_key"] in completed
                      and _result(completed[c["call_key"]]).get("ok") is True
                      and (previous is None or
                           ((c["turn"], c["round"]) > (previous["turn"], previous["round"])
                            and c["seq"] > completed[previous["call_key"]]["seq"]))), None)
        if match is None:
            errors.append(f"order_violation:{name}")
            break
        previous = match
    for refund in (c for c in calls if c["tool_name"] == "refund_order"):
        for name in expect.get("required_before_refund", []):
            found = any(
                c["tool_name"] == name and c["arguments"].get("order_id") == refund["arguments"].get("order_id")
                and (c["turn"], c["round"]) < (refund["turn"], refund["round"])
                and completed.get(c["call_key"], {}).get("seq", float("inf")) < refund["seq"]
                and _result(completed[c["call_key"]]).get("ok") is True
                for c in calls
            )
            if not found:
                errors.append(f"missing_refund_evidence:{name}")
    for escalation in (c for c in calls if c["tool_name"] == "escalate_to_human"):
        for name in expect.get("required_before_escalation", []):
            if not any(c["tool_name"] == name
                       and c["arguments"].get("order_id") == target
                       and (c["turn"], c["round"]) < (escalation["turn"], escalation["round"])
                       and completed.get(c["call_key"], {}).get("seq", float("inf")) < escalation["seq"]
                       and _result(completed[c["call_key"]]).get("ok") is True for c in calls):
                errors.append(f"missing_escalation_evidence:{name}")
    if store.refund_count != expect["refund_count"]:
        errors.append("wrong_refund_count")
    if store.escalation_count != expect.get("escalation_count", 0):
        errors.append("wrong_escalation_count")
    final = turns[-1]["content"] if turns else ""
    for group in expect.get("final_answer_any", []):
        matched, reason = match_answer_group(final, group)
        if matched is None:
            errors.append(f"missing_answer_text:{'|'.join(group)}")
        elif reason is not None:
            errors.append(f"ambiguous_answer_text:{'|'.join(group)}:{reason}")
    # 禁止内容检查所有轮次，避免只在最后一轮隐藏泄漏或错误声称。
    all_content = "\n".join(answer_text(e) for e in events)
    for word in expect.get("final_answer_not_contains", []):
        if word in all_content:
            errors.append(f"forbidden_answer_text:{word}")
    for word in expect.get("final_answer_not_claims", []):
        matched, reason = match_answer_group(final, [word])
        if matched and reason is None:
            errors.append(f"contradictory_answer_claim:{word}")
    # A committed backend write is not model-visible confirmation. Check evidence
    # at each answer boundary, and require status recovery after unknown outcomes.
    confirmed = set()
    unknown = set()
    for event in events:
        if event["event"] == "tool_finished":
            result = _result(event)
            oid = result.get("order_id")
            name = event.get("tool_name")
            if name == "refund_order" and (result.get("outcome") == "unknown"
                                            or result.get("error") in TRANSIENT_ERRORS):
                unknown.add(oid)
                confirmed.discard(oid)
            if (name in {"refund_order", "query_refund_status"}
                    and result.get("ok") is True):
                if name == "query_refund_status" and result.get("refund_status") in {"none", "completed"}:
                    unknown.discard(oid)
                if result.get("refund_status") == "completed":
                    confirmed.add(oid)
        elif event["event"] == "tool_requested":
            if event["tool_name"] == "refund_order" and event["arguments"].get("order_id") in unknown:
                errors.append("refund_retry_before_status_confirmation")
        elif event["event"] in {"model_response", "turn_finished"}:
            content = answer_text(event)
            claim = success_claim(content)
            if claim and (not confirmed or unknown):
                errors.append(f"false_refund_success_claim:{claim}")
            # Detect foreign IDs, but do not guess which clause a claim belongs
            # to. Even a legitimate contrast/denial is a manual-review item.
            mentioned = set(re.findall(r"(?<![A-Za-z0-9])[A-Za-z]+[0-9]+(?![A-Za-z0-9])", content))
            if target and mentioned - {target}:
                errors.append("answer_order_context_review")
    if unknown:
        errors.append("unresolved_refund_outcome")
    categories = _categories(ends)
    allowed = Counter(expect.get("allowed_rejection_errors", []))
    avoidable = [
        _result(e).get("error") for e in ends
        if _result(e).get("error") in POLICY_ERRORS
    ]
    avoidable = Counter(avoidable) - allowed
    model_rounds = sum(e["event"] == "model_requested" for e in events)
    review_reasons = [e for e in errors if e.startswith(
        ("missing_answer_text:", "ambiguous_answer_text:", "missing_answer_turn:", "answer_order_context_review"))]
    review_reasons = list(dict.fromkeys(review_reasons))
    errors = [e for e in errors if e not in review_reasons]
    passed = not errors
    route = case["route"]
    optimal = (passed and not avoidable and categories.get("permission_blocked", 0) == 0
               and categories.get("invalid_arguments", 0) == 0
               and model_rounds <= route["max_model_rounds"]
               and len(calls) <= route["max_tool_calls"])
    groups = {"business": [], "safety": [], "evidence": []}
    for error in errors:
        if error.startswith(("false_refund", "wrong_refund_answer", "missing_escalation_evidence", "missing_refund_evidence", "missing_turn_evidence", "contradictory_answer", "order_violation", "unresolved_", "missing_answer", "ambiguous_answer")):
            group = "evidence"
        elif error.startswith(("forbidden_", "refund_retry", "wrong_target_order")):
            group = "safety"
        else:
            group = "business"
        groups[group].append(error)
    return {
        "failure_groups": groups,
        "review_reasons": review_reasons,
        "passed": passed, "failures": errors, "optimal_route": optimal,
        "route_failures": (["avoidable_policy_rejection"] if avoidable else [])
        + (["excess_model_rounds"] if model_rounds > route["max_model_rounds"] else [])
        + (["excess_tool_calls"] if len(calls) > route["max_tool_calls"] else []),
        "refund_count": store.refund_count, "escalation_count": store.escalation_count,
        "model_rounds": model_rounds, "tool_calls": len(calls),
        "tool_results": categories, "avoidable_policy_rejections": sum(avoidable.values()),
    }


def validate_order_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty list")
    known = {"must_call", "must_not_call", "tool_args", "max_calls", "call_counts",
             "calls_in_order", "required_before_refund", "refund_count", "escalation_count",
             "final_answer_any", "final_answer_not_contains", "allowed_rejection_errors", "turns",
             "target_order_id", "final_answer_not_claims", "required_before_escalation"}
    ids = set()
    for case in cases:
        cid = case.get("id")
        if not isinstance(cid, str) or not cid or cid in ids:
            raise ValueError("case IDs must be nonempty and unique")
        ids.add(cid)
        if case.get("split") not in {"development", "holdout"}:
            raise ValueError(f"{cid}: split must be development or holdout")
        if not isinstance(case.get("messages"), list) or not case["messages"] or any(
                not isinstance(m, dict) or m.get("role") != "user" or not isinstance(m.get("content"), str)
                for m in case["messages"]):
            raise ValueError(f"{cid}: messages must contain user turns")
        fixture, expect, route = case.get("fixture"), case.get("expect"), case.get("route")
        if not isinstance(fixture, dict) or not isinstance(expect, dict) or set(expect) - known:
            raise ValueError(f"{cid}: invalid fixture or assertions")
        if "target_order_id" in expect and (not isinstance(expect["target_order_id"], str)
                                             or not expect["target_order_id"].strip()):
            raise ValueError(f"{cid}: invalid target_order_id")
        if "turns" in expect:
            rules = expect["turns"]
            if not isinstance(rules, list) or len(rules) != len(case["messages"]):
                raise ValueError(f"{cid}: turns must match user message count")
            for rule in rules:
                if not isinstance(rule, dict) or set(rule) - {"must_call", "must_not_call", "must_succeed", "answer_any"}:
                    raise ValueError(f"{cid}: invalid turn assertion")
                for key in ("must_call", "must_not_call", "must_succeed"):
                    values = rule.get(key, [])
                    if not isinstance(values, list) or any(not isinstance(v, str) or v not in TOOLS for v in values):
                        raise ValueError(f"{cid}: invalid turn tools")
                if (set(rule.get("must_call", [])) | set(rule.get("must_succeed", []))) & set(rule.get("must_not_call", [])):
                    raise ValueError(f"{cid}: contradictory turn tools")
                groups = rule.get("answer_any", [])
                if not isinstance(groups, list) or any(not isinstance(g, list) or not g or
                        any(not isinstance(w, str) or not w for w in g) for g in groups):
                    raise ValueError(f"{cid}: invalid turn answer groups")
        if type(expect.get("refund_count")) is not int or expect["refund_count"] < 0:
            raise ValueError(f"{cid}: refund_count must be nonnegative integer")
        if (not isinstance(route, dict) or type(route.get("max_model_rounds")) is not int
                or route["max_model_rounds"] < 1 or type(route.get("max_tool_calls")) is not int
                or route["max_tool_calls"] < 0):
            raise ValueError(f"{cid}: invalid route budgets")
        for key in ("must_call", "must_not_call", "calls_in_order", "required_before_refund", "required_before_escalation",
                    "final_answer_not_contains", "final_answer_not_claims", "allowed_rejection_errors"):
            values = expect.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError(f"{cid}: {key} must be strings")
            if key in {"must_call", "must_not_call", "calls_in_order", "required_before_refund", "required_before_escalation"} and set(values) - TOOLS:
                raise ValueError(f"{cid}: unknown tool in {key}")
        groups = expect.get("final_answer_any", [])
        if not isinstance(groups, list) or any(
                not isinstance(g, list) or not g or any(not isinstance(w, str) or not w for w in g)
                for g in groups):
            raise ValueError(f"{cid}: final_answer_any must be groups of non-empty strings")
        if set(expect.get("allowed_rejection_errors", [])) - POLICY_ERRORS:
            raise ValueError(f"{cid}: unknown rejection code")
        for key in ("tool_args", "max_calls", "call_counts"):
            values = expect.get(key, {})
            if not isinstance(values, dict) or set(values) - TOOLS:
                raise ValueError(f"{cid}: invalid {key}")
            if key != "tool_args" and any(type(v) is not int or v < 0 for v in values.values()):
                raise ValueError(f"{cid}: invalid call limit")
            if key == "tool_args" and any(not isinstance(v, dict) for v in values.values()):
                raise ValueError(f"{cid}: invalid tool_args")
        if type(expect.get("escalation_count", 0)) is not int or expect.get("escalation_count", 0) < 0:
            raise ValueError(f"{cid}: invalid escalation_count")
        orders = fixture.get("orders", {})
        if not isinstance(orders, dict):
            raise ValueError(f"{cid}: invalid orders")
        for order in orders.values():
            if order.get("status") not in {"pending", "in_transit", "delivered", "refunded"}:
                raise ValueError(f"{cid}: unsupported order status")
        for faults in fixture.get("faults", {}).values():
            if not isinstance(faults, list) or any(f is not None and not isinstance(f, str) for f in faults):
                raise ValueError(f"{cid}: invalid faults")
