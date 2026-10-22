# README hero GIF: the live arena view on a fake-provider match ($0, no network, no API key).
# Re-render from the repo root: vhs docs/demo.tape   (needs vhs; writes docs/demo.gif, keep it under 2 MB)
Output docs/demo.gif
Set Shell bash
Set FontSize 16
Set Width 1000
Set Height 560
Set Framerate 12
Set TypingSpeed 30ms
Hide
Type "export D=$(mktemp -d) && cd $D && unset OPENAI_API_KEY NO_COLOR FORCE_COLOR VIRTUAL_ENV_PROMPT && export DUEL_SANDBOX=subprocess DUEL_ROUNDS=2 DUEL_LOG_LEVEL=WARNING && PS1='$ ' && clear"
Enter
Show
Type "duel run --match-id demo --pace 0.1"
Enter
Sleep 12.5s
