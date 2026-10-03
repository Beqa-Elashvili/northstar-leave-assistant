"""Deterministic Georgian rendering of MCP results (numbers never pass through the LLM)."""

from __future__ import annotations

LEAVE_TYPE_NAMES = {
    "ANNUAL": "ყოველწლიური ანაზღაურებადი შვებულება",
    "SICK": "ავადმყოფობის შვებულება",
    "UNPAID": "უხელფასო შვებულება",
    "BEREAVEMENT": "გლოვის შვებულება",
    "STUDY": "სასწავლო და საგამოცდო შვებულება",
    "PARENTAL": "მშობლის შვებულება",
}
STATUS_NAMES = {"pending": "განხილვის პროცესში", "approved": "დამტკიცებული", "rejected": "უარყოფილი",
                "cancelled": "გაუქმებული"}
UNIT_NAMES = {"working": "სამუშაო დღე", "calendar": "კალენდარული დღე"}
CHANNEL_NAMES = {"hr": "ადამიანური რესურსების სამსახური", "hr_portal": "HR პორტალი ან ადამიანური რესურსების სამსახური",
                 "manager": "უშუალო ხელმძღვანელი"}

POLICY = "შვებულებისა და გაცდენის პოლიტიკა v4.0"


def type_name(code: str | None) -> str:
    return LEAVE_TYPE_NAMES.get(code or "", code or "")


def days_label(days: int | None, unit: str | None) -> str:
    return f"{days} {UNIT_NAMES.get(unit or '', 'დღე')}"


def format_balances(data: dict) -> str:
    lines = [f"თქვენი შვებულების ბალანსი ({data['year']} წელი):", ""]
    for b in data["balances"]:
        unit = UNIT_NAMES.get(b.get("unit") or "", "დღე")
        lines.append(f"• {type_name(b['leave_type'])}")
        lines.append(f"    კუთვნილი: {b['entitled_days']}" +
                     (f" + გადმოტანილი: {b['carried_over_days']}" if b["carried_over_days"] else ""))
        lines.append(f"    დამტკიცებული: {b['approved_days']}  |  განხილვის პროცესში: {b['pending_days']}")
        available = f"    ხელმისაწვდომი: {b['available_days']} {unit}"
        if b["leave_type"] == "SICK":
            available += " (ანაზღაურებადი)"
        lines.append(available)
    if any(b["leave_type"] == "SICK" for b in data["balances"]):
        lines += ["", "შენიშვნა: ავადმყოფობის 10 დღე ანაზღაურების ლიმიტია და არა ავადმყოფობის აღრიცხვის მაქსიმუმი "
                      "(მუხლი 6.4)."]
    if any(b["pending_days"] for b in data["balances"]):
        lines += ["განხილვის პროცესში მყოფი დღეები ბალანსს წინასწარ აკლდება (მუხლი 5.1)."]
    return "\n".join(lines)


def format_requests(data: dict) -> str:
    rows = data["requests"]
    if not rows:
        return "ასეთი მოთხოვნები ვერ მოიძებნა."
    lines = ["თქვენი შვებულების მოთხოვნები:", ""]
    for r in rows:
        lines.append(f"• #{r['request_id']}  {type_name(r['leave_type'])}  {r['start_date']} – {r['end_date']}  "
                     f"({r['days']} დღე)  —  {STATUS_NAMES.get(r['status'], r['status'])}")
    return "\n".join(lines)


def format_proposal(p: dict) -> str:
    lines = ["გთხოვთ დაადასტუროთ:", "",
             f"ტიპი: {p['leave_type_name']}",
             f"პერიოდი: {p['start_date']} – {p['end_date']}",
             f"დღეების რაოდენობა: {days_label(p['days'], p['day_unit'])}"]
    if p.get("comment"):
        lines.append(f"მიზეზი: {p['comment']}")
    if p.get("available_days_before") is not None:
        lines.append(f"ბალანსი: {p['available_days_before']} → {p['available_days_after']}")
    lines += ["", "შექმნილი მოთხოვნა მიიღებს სტატუსს „განხილვის პროცესში“ — ეს დამტკიცებას არ ნიშნავს (მუხლი 12.2).",
              "", "შევქმნა მოთხოვნა? (დიახ/არა)"]
    return "\n".join(lines)


def format_violations(p: dict) -> str:
    lines = ["მოთხოვნის შექმნა ასისტენტით შეუძლებელია:", ""]
    for v in p["violations"]:
        line = f"• {v['message']} ({POLICY}, მუხლი {v['article']})"
        if v.get("redirect_to"):
            line += f"\n  მიმართეთ: {CHANNEL_NAMES[v['redirect_to']]}."
        lines.append(line)
    return "\n".join(lines)


def format_created(result: dict) -> str:
    r = result["request"]
    if result.get("already_existed"):
        head = f"ეს მოთხოვნა უკვე შექმნილია — ხელახლა არ შემიქმნია. მოთხოვნის ნომერი: #{r['request_id']}."
    else:
        head = f"მოთხოვნა შეიქმნა. მოთხოვნის ნომერი: #{r['request_id']}."
    text = (f"{head}\n{type_name(r['leave_type'])}, {r['start_date']} – {r['end_date']} ({r['days']} დღე)\n"
            f"სტატუსი: {STATUS_NAMES.get(r['status'], r['status'])}")
    if r["status"] == "pending":
        text += " — ეს დამტკიცებას არ ნიშნავს (მუხლი 12.2)."
        note = _NEXT_STEPS.get(r["leave_type"])
        if r["leave_type"] == "SICK" and r["days"] <= 2:
            note = None
        if note:
            text += f"\n{note}"
    return text


# What happens after submission, per leave type, as the policy states it.
_NEXT_STEPS = {
    "ANNUAL": "გადაწყვეტილებას უშუალო ხელმძღვანელი იღებს. დამტკიცებამდე მგზავრობისა და სხვა ხარჯების გადახდისგან "
              "თავი შეიკავეთ (მუხლი 4.4).",
    "UNPAID": "უხელფასო შვებულებას ჯერ უშუალო ხელმძღვანელი ამტკიცებს, შემდეგ ადამიანური რესურსების სამსახური; "
              "მოთხოვნა დამტკიცებულად ორივე თანხმობის შემდეგ ითვლება (მუხლი 7.3).",
    "SICK": "ავადმყოფობა 2 სამუშაო დღეზე მეტხანს გრძელდება, ამიტომ სამსახურში დაბრუნებიდან 5 სამუშაო დღის "
            "განმავლობაში HR პორტალში ატვირთეთ ან HR-ს გაუგზავნეთ სამედიცინო ცნობა (მუხლი 6.3).",
}
