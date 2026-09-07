from django import forms
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.core.responses import protect_sensitive_html
from apps.operations.audit import ALLOWED_ACTIONS, _hash
from apps.operations.models import AuditEvent
from .access import Capability, authorize_patient
from .models import PatientMembership


ACTION_LABELS = {
    "patient_viewed": "查看患者资料", "document_viewed": "查看资料", "source_viewed": "查看原件来源",
    "fact_viewed": "查看事实", "lab_viewed": "查看检验", "export_viewed": "查看导出",
    "original_downloaded": "下载原件", "export_downloaded": "下载导出文件", "audit_viewed": "查看访问记录",
    "members_viewed": "查看家庭成员", "invitation_viewed": "查看邀请", "share_viewed": "查看分享",
    "notification_viewed": "查看任务通知", "review_viewed": "查看复核", "access_attempted": "操作请求",
    "document_uploaded": "上传资料", "upload_started": "开始上传", "upload_removed": "移除上传项",
    "lab_revised": "核对检验", "fact_added": "补充事实", "fact_revised": "核对事实",
    "document_trashed": "移入回收站", "document_restored": "恢复资料", "document_deletion_requested": "请求删除资料",
    "document_deletion_purged": "清理已删除资料", "processing_requeued": "重新解析", "parsing_version_activated": "切换解析版本",
    "member_role_changed": "变更成员权限", "member_access_revoked": "移除成员", "patient_name_changed": "修改患者称呼",
    "patient_created": "创建患者", "patient_deletion_requested": "删除患者", "notification_preference_changed": "修改通知偏好",
    "push_subscription_created": "开启推送", "push_subscription_revoked": "关闭推送",
    "inaccuracy_feedback_created": "反馈识别问题", "product_feedback_created": "提交产品意见",
    "account_deletion_requested": "注销账号", "account_deletion_purged": "清理注销账号",
    "dictionary_published": "发布字典", "quota_changed": "调整配额", "support_access_granted": "授权支持访问",
    "support_access_used": "支持访问", "deletion_status_viewed": "查看删除状态",
    "export_preview_created": "准备导出", "export_requested": "生成导出", "export_generated": "完成导出",
    "export_cancelled": "取消导出", "invitation_created": "创建邀请", "invitation_accepted": "接受邀请",
    "invitation_revoked": "撤销邀请", "share_created": "创建分享", "share_revoked": "撤销分享",
    "share_access_granted": "打开分享链接", "share_expired": "分享到期", "share_invalidated": "分享失效",
}
RESULT_LABELS = {"succeeded": "成功", "denied": "已拒绝", "failed": "未完成", "scheduled": "已开始"}
RESOURCE_LABELS = {"patient": "患者", "document": "资料", "fact": "事实", "lab_observation": "检验",
                   "parsing_version": "解析版本", "member": "成员", "invitation": "邀请", "share": "分享",
                   "export": "导出", "notification": "通知", "review": "复核", "feedback": "反馈",
                   "upload_batch": "上传批次", "upload_item": "上传项", "support": "支持访问", "quota": "配额"}


class AuditFilterForm(forms.Form):
    action = forms.ChoiceField(label="操作", required=False, choices=[("", "全部操作")] + [(key, ACTION_LABELS.get(key, "其他操作")) for key in sorted(ALLOWED_ACTIONS)])
    resource_type = forms.ChoiceField(label="资源类型", required=False, choices=[("", "全部类型")] + list(RESOURCE_LABELS.items()))
    result = forms.ChoiceField(label="结果", required=False, choices=[("", "全部结果")] + list(RESULT_LABELS.items()))
    actor = forms.ChoiceField(label="操作者", required=False)
    resource_id = forms.UUIDField(label="资源编号", required=False)
    start = forms.DateField(label="开始日期", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="结束日期", required=False, widget=forms.DateInput(attrs={"type": "date"}))


@login_required
@require_GET
def audit_history(request, patient_id):
    access = authorize_patient(patient_id, request.user, Capability.MANAGE)
    request.patient, request.patient_access = access.patient, access
    events = AuditEvent.objects.filter(patient_hash=_hash("patient", access.patient.pk)).order_by("-created_at", "-pk")
    names = {_hash("actor", "system"): "系统任务", _hash("actor", "anonymous"): "未登录访问"}
    for member in PatientMembership.objects.filter(patient=access.patient):
        names[_hash("actor", member.account_id)] = "创建者" if member.account_id == access.patient.account_id else (member.label or f"成员 {str(member.account_id)[:8]}")
    actors = list(events.order_by().values_list("actor_hash", flat=True).distinct())
    names.update({key: f"访问者 {key[:8]}" for key in actors if key not in names})
    form = AuditFilterForm(request.GET)
    form.fields["actor"].choices = [("", "全部操作者")] + [(key, names[key]) for key in actors]
    if form.is_valid():
        data = form.cleaned_data
        for field in ("action", "resource_type", "result"):
            if data[field]:
                events = events.filter(**{field: data[field]})
        if data["actor"]:
            events = events.filter(actor_hash=data["actor"])
        if data["resource_id"]:
            events = events.filter(target_hash=_hash("target", data["resource_id"]))
        if data["start"]:
            events = events.filter(created_at__date__gte=data["start"])
        if data["end"]:
            events = events.filter(created_at__date__lte=data["end"])
    else:
        events = events.none()
    page = Paginator(events, 50).get_page(request.GET.get("page"))
    for event in page:
        event.actor_label = names.get(event.actor_hash, f"访问者 {event.actor_hash[:8]}")
        event.action_label = ACTION_LABELS.get(event.action, "历史操作")
        event.resource_label = RESOURCE_LABELS.get(event.resource_type, "其他")
        event.result_label = RESULT_LABELS.get(event.result, "历史结果")
    query = request.GET.copy()
    query.pop("page", None)
    response = render(request, "patients/audit_history.html", {"form": form, "events": page, "filter_query": query.urlencode()}, status=200 if form.is_valid() else 400)
    authorize_patient(patient_id, request.user, Capability.MANAGE)
    return protect_sensitive_html(response)
