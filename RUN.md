# Run the XFed demo

Run these commands from the repository root after starting Ollama. Use the
activation command for your shell:

    source .venv/bin/activate
Linux, WSL, or another Unix environment.

    source .venv/Scripts/activate
Git Bash on Windows.

    python -m pip install -r requirements.txt
Installs the Python dependencies.

    python src/algorithmic/run_policies.py contract_003
Extracts the deterministic policy baseline.

    python src/llm/policies/extract_policies_deepseek.py
Extracts policy rules with DeepSeek.

    python src/llm/policies/extract_policies_qwen.py
Extracts policy rules with Qwen.

    python src/llm/policies/merge_policies.py
Checks the candidates against the contract and creates the merged policy.

    python src/llm/workflows/generate_workflows.py --model all
Generates DeepSeek and Qwen workflow proposals, then merges them with Llama.

    python src/llm/workflows/check_workflow.py
Checks the merged workflow against the merged policy.

The checker defaults to the merged proposal. To check one proposal, use for example:

    python src/llm/workflows/check_workflow.py --workflow-source llm1_deepseek

The compliance result is saved to data/contracts/contract_003/workflow/workflow_check.json.
