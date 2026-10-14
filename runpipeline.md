# Run Pipeline

## 1. UI - Gorina5

```bash
cd ~/git/xfed-policy-workflow-dataset
git pull --ff-only
"$HOME/.conda/envs/xfed/bin/python" -m pip install -e .
"$HOME/.conda/envs/xfed/bin/python" -m xfed.ui
```

## 2. Tunnel - ssh1

In a separate terminal on your computer:

```bash
ssh ohassane@ssh1.ux.uis.no
```

Then, on ssh1:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8875:127.0.0.1:8765 gorina5
```

## 3. Tunnel - Windows PowerShell

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8875:127.0.0.1:8875 ohassane@ssh1.ux.uis.no
```

Keep the UI and both tunnels running. Blank tunnel terminals are normal.

## 4. Browser

Open [http://127.0.0.1:8875](http://127.0.0.1:8875).
Select the contract and model. Run 1.1, 1.2, 1.3, 1.4, then 2.

## 5. GPU Monitor - Another Gorina5 Terminal

```bash
watch -n 1 nvidia-smi
```

Installed / loaded models:

```bash
curl -sS http://localhost:11434/api/tags
curl -sS http://localhost:11434/api/ps
```