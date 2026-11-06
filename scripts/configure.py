"""Generate absolute plugin paths for this checkout; no changes to Harness internals."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def generate():
    def package(name):
        return (ROOT / "node_modules" / "@deepseek-ai" / name / "lib" / "index.js").as_posix()
    disabled = ["persistent-bash", "persistent-pwsh", "tool-fs", "tool-working-directory", "compaction-basic", "image-offload"]
    patch = [{"id": name, "disabled": True} for name in disabled]
    # SDK JSON-RPC creates preset-free roots; use the public deployment persona
    # configuration. Explicit native child presets still override this scope.
    patch.append({'id': 'system-prompt', 'config': {
        'personaPrefix': (ROOT / 'prompts' / 'enterprise-main.txt').read_text(encoding='utf-8'),
        'includeRuntimeContext': False, 'includeHarnessIdentity': False}})
    patch.append({"insert": [
        {"id": "enterprise-presets", "name": package("dsh-agent-preset-registry"), "config": {"default": "enterprise-main"}},
        *[{"id": f"preset-{role}", "name": package("dsh-agent-preset"), "inject": ["systemPrompt"], "config": {
            "id": role, "plugins": [{"id": f"persona-{role}", "name": Path(package("dsh-persona")).as_uri(), "config": {
                "prefix": (ROOT / "prompts" / f"{role}.txt").read_text(encoding="utf-8"),
                "complete": True, "includeRuntimeContext": False}}]}}
          for role in ["enterprise-main", "business-analysis", "inventory-analysis", "procurement-analysis", "enterprise-assistant"]],
        {"id": "enterprise-subagents", "name": package("dsh-subagent")},
        {"id": "enterprise-spawn", "name": package("dsh-subagent-spawn-in-process"), "config": {"providerName": "enterprise-spawn"}},
        {"id": "enterprise-business", "name": (ROOT / "plugins" / "business.mjs").as_posix()},
    ]})
    mock = [{"id": "llm-deepseek", "disabled": True}, {"insert": [
        {"id": "enterprise-test-model", "name": (ROOT / "plugins" / "mock-model.mjs").as_posix()},
    ]}]
    (ROOT / "config").mkdir(exist_ok=True)
    assistant = [{'id': 'enterprise-presets', 'config': {'default': 'enterprise-assistant'}}]
    assistant.append({'id': 'system-prompt', 'config': {
        'personaPrefix': (ROOT / 'prompts' / 'enterprise-assistant.txt').read_text(encoding='utf-8')}})
    assistant.append({'insert': [{'id': 'enterprise-assistant-tasks', 'name': (ROOT / 'plugins' / 'assistant-tasks.mjs').as_posix()}]})
    for name, document in [("enterprise", patch), ("mock", mock), ('assistant', assistant)]:
        (ROOT / "config" / f"{name}.patch.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Generated enterprise and test patches")


if __name__ == "__main__":
    generate()
