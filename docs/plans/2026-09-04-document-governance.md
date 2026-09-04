# Document Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move canonical documentation under `docs/`, establish a governed document catalog and release manifests, and make repository automation reject future drift.

**Architecture:** Human readers enter through `docs/README.md`; automation reads `docs/document-registry.json`. A standard-library Python verifier checks placement, schema, references, release manifests, and integration points. `CHANGELOG.md` remains Release Please-owned, while release manifests connect versions to design, implementation, evidence, and the production gate.

**Tech Stack:** Markdown, JSON, Python 3.11 standard library, pytest, GitHub Actions

**Spec:** `docs/specs/2026-09-04-document-governance-design.md`

**执行结果（2026-09-04）：** 全部任务已完成并通过自动验证；当前版本关联尚未由
Release Please 确定，因此登记表中的 `releases` 保持为空。

## Global Constraints

- Root Markdown is limited to `README.md`, `CHANGELOG.md`, `AGENTS.md`, `LICENSE*`, and `NOTICE*`; only the platform template paths allowed by the governance policy are exempt under `.github/`.
- Canonical product, decision, specification, plan, policy, release, verification, deployment, and license documents live under `docs/`.
- Preserve historical content; use tracked moves and update every repository reference.
- `CHANGELOG.md` and version fields remain controlled by Release Please.
- GitHub Release does not imply production approval; the release gate remains authoritative.
- Do not use historical plan checkboxes as live delivery status.

---

### Task 1: Migrate canonical documents into the governed directory tree

**Files:**
- Move: `产品方案-v2.0-评审完善稿.md` → `docs/product/产品方案-v2.0-评审完善稿.md`
- Move: `第一版产品需求文档-PRD-v1.0.md` → `docs/product/第一版产品需求文档-PRD-v1.0.md`
- Move: `方案审查结论.md` → `docs/decisions/方案审查结论.md`
- Move: `docs/superpowers/specs/*.md` → `docs/specs/*.md`
- Move: `docs/superpowers/plans/*.md` → `docs/plans/*.md`
- Move: `docs/versioning.md` → `docs/policies/versioning.md`
- Move: `deploy/README.md` → `docs/deployment/local-development.md`
- Move: `deploy/runbook.md` → `docs/deployment/production-runbook.md`
- Modify: `README.md`
- Modify: `tools/verify_traceability.py`
- Modify: `docs/verification/traceability.json`
- Modify: `docs/verification/traceability.md`
- Modify: `docs/verification/backup-restore.md`
- Modify: `tests/deploy/test_release_artifacts.py`
- Modify: migrated specifications and plans containing old paths

**Interfaces:**
- Consumes: existing Markdown paths and traceability source contract
- Produces: stable canonical paths under `docs/`

- [x] **Step 1: Move files without changing their historical prose**

Use tracked file moves and preserve UTF-8 content. The destination tree is:

```text
docs/product/
docs/decisions/
docs/specs/
docs/plans/
docs/policies/
docs/deployment/
```

- [x] **Step 2: Replace repository references with canonical paths**

Required replacements include:

```text
第一版产品需求文档-PRD-v1.0.md -> docs/product/第一版产品需求文档-PRD-v1.0.md
产品方案-v2.0-评审完善稿.md -> docs/product/产品方案-v2.0-评审完善稿.md
docs/superpowers/specs/ -> docs/specs/
docs/superpowers/plans/ -> docs/plans/
docs/versioning.md -> docs/policies/versioning.md
deploy/README.md -> docs/deployment/local-development.md
deploy/runbook.md -> docs/deployment/production-runbook.md
```

Relative Markdown links must be recalculated from each moved file, not replaced as raw text when the source directory changed.

- [x] **Step 3: Verify traceability and deployment artifact paths**

Run:

```powershell
python tools/verify_traceability.py
python -m pytest tests/deploy/test_release_artifacts.py -q
```

Expected: traceability reports 62 requirements and the deployment artifact test passes.

- [x] **Step 4: Verify no legacy paths remain**

Run:

```powershell
rg -n "docs/superpowers|docs/versioning\.md|deploy/(README|runbook)\.md" .
rg -n "\]\((产品方案-v2\.0-评审完善稿|第一版产品需求文档-PRD-v1\.0)\.md" .
```

Expected: no active references; only the governance design and this migration plan may mention legacy
paths as historical migration input.

- [x] **Step 5: Commit the controlled migration**

```powershell
git add -A -- .
git commit -m "docs(governance): 统一文档目录结构"
```

### Task 2: Add the human index, policy, registry, and release manifests

**Files:**
- Create: `docs/README.md`
- Create: `docs/policies/document-governance.md`
- Create: `docs/document-registry.json`
- Create: `docs/releases/v0.1.0.md`
- Create: `docs/releases/v0.2.0.md`
- Create: `docs/releases/v0.2.1.md`
- Create: `docs/releases/v0.3.0.md`

**Interfaces:**
- Consumes: canonical paths from Task 1, `CHANGELOG.md`, tags `v0.2.0`–`v0.3.0`, and verification evidence
- Produces: one human entry point and one machine-readable source of document status

- [x] **Step 1: Write the normative policy**

The policy must define directory ownership, naming, `lifecycle`, `delivery`, required registry fields, release responsibilities, migration/archive rules, and the PR checklist. It must explicitly state:

```text
计划复选框是执行日志，不是当前进度；当前状态以登记表和验证证据为准。
```

- [x] **Step 2: Create release manifests without duplicating the Changelog**

Each `docs/releases/vX.Y.Z.md` contains these headings:

```markdown
# vX.Y.Z
## 版本身份
## 变更范围
## 关联文档
## 验证与部署结论
```

Each manifest links `../../CHANGELOG.md` and `../verification/release-gate.md`. `v0.1.0` records the missing historical tag; `v0.3.0` records production deployment as `BLOCKED`.

- [x] **Step 3: Create the complete JSON registry**

Use this exact entry shape:

```json
{
  "id": "spec-document-governance",
  "title": "文档治理与版本关联设计",
  "path": "docs/specs/2026-09-04-document-governance-design.md",
  "kind": "spec",
  "lifecycle": "active",
  "delivery": "implementing",
  "owner": "engineering",
  "releases": [],
  "supersedes": [],
  "implementation_refs": [],
  "evidence": []
}
```

Register root `README.md` and `CHANGELOG.md` plus every `docs/**/*.md`. Do not register generated verification JSON as human documents.

- [x] **Step 4: Create the human index from registry facts**

`docs/README.md` explains the reading order and state model, links every registered document, and highlights current source version `0.3.0` separately from the `BLOCKED` production gate.

- [x] **Step 5: Check JSON and Markdown references**

Run:

```powershell
python -m json.tool docs/document-registry.json > $null
git diff --check
```

Expected: both commands exit 0.

- [x] **Step 6: Commit governance artifacts**

```powershell
git add docs
git commit -m "docs(governance): 建立文档登记与版本清单"
```

### Task 3: Implement the documentation verifier with TDD

**Files:**
- Create: `tests/tools/test_documentation.py`
- Create: `tools/verify_documentation.py`

**Interfaces:**
- Produces: `verify(root: Path) -> int`-compatible command behavior and `DocumentationError`
- CLI: `python tools/verify_documentation.py [--root PATH]`

- [x] **Step 1: Write failing happy-path and missing-registration tests**

```python
def test_complete_documentation_repository_is_accepted(tmp_path):
    write_documentation_repo(tmp_path)
    result = run_verifier(tmp_path)
    assert result.returncode == 0, result.stderr


def test_verifier_rejects_an_unregistered_markdown_file(tmp_path):
    write_documentation_repo(tmp_path)
    (tmp_path / "docs" / "orphan.md").write_text("# Orphan\n", encoding="utf-8")
    result = run_verifier(tmp_path)
    assert result.returncode == 1
    assert "unregistered Markdown" in result.stderr
```

- [x] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/tools/test_documentation.py -q
```

Expected: FAIL because `tools/verify_documentation.py` does not exist.

- [x] **Step 3: Implement registry loading, placement, and schema validation**

The implementation defines:

```python
class DocumentationError(ValueError):
    pass


def verify(root: Path) -> int:
    registry = _load_registry(root)
    _validate_entries(root, registry)
    _validate_markdown_inventory(root, registry)
    _validate_relationships(root, registry)
    _validate_release_manifests(root, registry)
    _validate_integrations(root, registry)
    return len(registry["documents"])
```

Use `json`, `pathlib`, `posixpath`, and `re` only. Reject absolute paths, `..`, backslashes, duplicates, invalid enums, broken evidence, invalid implementation references, and Markdown outside allowed locations.

- [x] **Step 4: Run tests and verify GREEN**

Run:

```powershell
python -m pytest tests/tools/test_documentation.py -q
```

Expected: PASS.

- [x] **Step 5: Add failing relationship and release tests**

Cover invalid lifecycle/delivery values, broken `supersedes`, missing evidence for verified specifications, malformed commit references, missing release manifests, broken index links, and Markdown under `deploy/`.

- [x] **Step 6: Run the new tests and verify RED, then implement minimal checks**

Run before and after implementation:

```powershell
python -m pytest tests/tools/test_documentation.py -q
```

Expected before: new cases fail for their intended reasons. Expected after: all cases pass.

- [x] **Step 7: Verify the real repository**

Run:

```powershell
python tools/verify_documentation.py
```

Expected output:

```text
Documentation verified: 42 registered Markdown documents
```

- [x] **Step 8: Commit verifier and tests**

```powershell
git add tools/verify_documentation.py tests/tools/test_documentation.py
git commit -m "test(governance): 校验文档目录与关联"
```

### Task 4: Make AI, PR, README, and CI workflows enforce the policy

**Files:**
- Modify: `AGENTS.md`
- Modify: `.github/pull_request_template.md`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `tools/verify_release_automation.py`
- Modify: `tests/tools/test_release_automation.py`

**Interfaces:**
- Consumes: `python tools/verify_documentation.py`
- Produces: mandatory AI instructions, contributor prompts, and CI enforcement

- [x] **Step 1: Extend release automation tests first**

Add `tools/verify_documentation.py` to the required CI commands in the test fixture and repository workflow expectations. Run:

```powershell
python -m pytest tests/tools/test_release_automation.py -q
```

Expected before production change: FAIL because the release verifier does not require the new command.

- [x] **Step 2: Add documentation rules to `AGENTS.md`**

Rules must require canonical `docs/` placement, the root allowlist, standard directories and names, registry/index updates, evidence-backed delivery status, release manifest linkage after version assignment, and `python tools/verify_documentation.py` before completion.

- [x] **Step 3: Update PR and project entry points**

The PR template adds fields for requirement/decision, spec, plan, evidence, Changelog impact, and a checklist item for registry/index/release manifest updates. Root README links `docs/README.md` instead of maintaining a competing full catalog and lists the documentation verifier with quality commands.

- [x] **Step 4: Add the verifier to CI and its release contract**

Add this command to the repository contract step:

```yaml
python tools/verify_documentation.py
```

Update `tools/verify_release_automation.py` to require it, then update the release automation test fixture.

- [x] **Step 5: Run focused integration checks**

```powershell
python -m pytest tests/tools/test_release_automation.py tests/tools/test_documentation.py -q
python tools/verify_release_automation.py
python tools/verify_documentation.py
```

Expected: all exit 0.

- [x] **Step 6: Commit workflow integration**

```powershell
git add AGENTS.md .github README.md tools/verify_release_automation.py tests/tools/test_release_automation.py
git commit -m "ci(governance): 强制执行文档管理规范"
```

### Task 5: Run final repository verification

**Files:**
- Modify only if verification finds an in-scope documentation defect

**Interfaces:**
- Consumes: all prior tasks
- Produces: fresh evidence for handoff

- [x] **Step 1: Check repository state and legacy paths**

```powershell
git status --short
git diff main...HEAD --check
rg -n "docs/superpowers|docs/versioning\.md|deploy/(README|runbook)\.md" . `
  --glob "!docs/specs/2026-09-04-document-governance-design.md" `
  --glob "!docs/plans/2026-09-04-document-governance.md"
```

- [x] **Step 2: Run repository contract checks**

```powershell
python tools/verify_documentation.py
python tools/verify_release_automation.py
python tools/release_version.py check
python tools/verify_traceability.py
python tools/verify_release_gate.py
python manage.py check
python manage.py makemigrations --check --dry-run
```

- [x] **Step 3: Run complete automated tests**

```powershell
python -m pytest -q
npm run test:js
```

Expected in the clean worktree: Python passes with only explicit external-environment skips; JavaScript reports 6 passing tests.

- [x] **Step 4: Review requirements line by line**

Confirm the directory policy, complete registry, release association, AI instructions, PR prompts, CI command, and unchanged `BLOCKED` production conclusion against the specification.

- [x] **Step 5: Record the final branch state**

```powershell
git status --short
git log --oneline main..HEAD
```

Expected: clean worktree and a reviewable sequence of documentation-governance commits.
