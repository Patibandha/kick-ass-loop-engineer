# Publishing kickAssLoopEngineer to GitHub

So others can install and use it as a Claude Code skill.

## One-time setup

```bash
cd loop-engineer
git init
git add .
git commit -m "kick-ass-loop-engineer: model-agnostic build/review loop as a Claude Code skill"
git branch -M main
```

Create a repo on GitHub (private to start, public when ready), then:

```bash
git remote add origin https://github.com/Patibandha/loop-engineer.git
git push -u origin main
git tag v1.0.0-alpha.1 && git push --tags
```

## How others install it

```bash
git clone https://github.com/Patibandha/loop-engineer.git
cd loop-engineer
pip install -e . --break-system-packages
cp -r .claude/skills/loop-engineer ~/.claude/skills/   # makes /loop-engineer global
loop-engineer init                                      # set their Ollama model
```

In a Claude Code session: `/loop-engineer <objective>`.

## Going public later

```bash
# flip repo visibility to public in GitHub settings, then optionally:
git tag v1.0.0 && git push --tags
```

Consider adding `CONTRIBUTING.md` and a CI workflow (`.github/workflows/ci.yml`
running `python -m unittest discover -s tests`) before announcing it.
