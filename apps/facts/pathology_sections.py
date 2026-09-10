"""Printed section roles shared by report boundaries and field extraction."""
import re


SUPPLIED_INFORMATION = re.compile(r"^(?:(?:受检者|患者|病人)(?:基本|临床)?信息|基本信息|送检(?:信息|资料)|临床(?:信息|资料))[:：]?$")
EXCLUDED_RESULT_SECTION = re.compile(r"^(?:样本质控(?:结果)?|质控(?:结果)?|质量控制(?:结果)?|阳性对照|阴性对照|检测图谱|染色图像|染色图谱|检测说明|说明|备注)(?:[:：]|$)")
CURRENT_RESULT_SECTION = re.compile(r"^(?:(?:病理|组织学)?诊断(?:结果|意见)|检测结果|染色结果|免疫(?:组织化学|组化)结果)(?:[:：]|$)")
