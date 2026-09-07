"""Conservative report-date context for page-bounded facts in mixed report bundles."""
from collections import defaultdict
import re


_LABEL = re.compile(r"(?:报\s*告\s*(?:日\s*期|时\s*间)|记\s*录\s*时\s*间|签\s*署\s*日\s*期)\s*[:：]")


def page_record_dates(lines):
    from .extraction import explicit_dates
    pages = {page for page,_,_ in lines}
    dates, raw_labels = defaultdict(list), defaultdict(list)
    for page,text,_ in lines:
        for label in _LABEL.finditer(text):
            # Read the first explicit date following its own label, never a treatment date elsewhere.
            following = text[label.end():]
            following = re.sub(r"((?:19|20)\d{2}[./-]\d{1,2}[./-]\d{2})(?=\d{2}[:：]\d{2})", r"\1 ", following)
            dates_in_line = explicit_dates(following)
            raw_labels[page].append(text[label.start():])
            if dates_in_line and following.lstrip().startswith(dates_in_line[0]["raw"]):
                dates[page].append(dates_in_line[0])
    if not raw_labels:
        return {}
    unique = {(item["value"],item["precision"]):item for values in dates.values() for item in values}
    output={}
    for page in pages:
        local={(item["value"],item["precision"]):item for item in dates[page]}
        if len(local)==1:
            output[page]={**next(iter(local.values())),"conflict":False}
        elif len(local)>1:
            output[page]={"raw":"；".join(raw_labels[page]),"value":None,"precision":"UNKNOWN","conflict":True}
        elif page not in raw_labels and len(unique)==1:
            output[page]={**next(iter(unique.values())),"conflict":False}
        else:
            output[page]={"raw":"；".join(raw_labels[page]),"value":None,"precision":"UNKNOWN","conflict":False}
    return output
