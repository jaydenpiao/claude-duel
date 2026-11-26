# `duel duplicate` GIF: mirrored boards on the fake provider ($0, no network, no API key).
# 6 boards: enough for a decisive call (6-0 gives p=0.0312); 12 boards currently stops at the 200-call run cap on the fake provider.
# Re-render from the repo root: vhs docs/duplicate.tape   (needs vhs and `duel` on PATH; keep it under 2 MB)
Output docs/duplicate.gif
Set Shell bash
Set FontSize 16
Set Width 1300
Set Height 520
Set Framerate 12
Set TypingSpeed 30ms
Hide
Type `export D=$(mktemp -d) && cd $D && unset OPENAI_API_KEY NO_COLOR FORCE_COLOR VIRTUAL_ENV_PROMPT && export DUEL_SANDBOX=subprocess DUEL_MODE=ladder DUEL_LOG_LEVEL=WARNING && PS1='$ ' && printf '[[agents]]\nname = "ember"\nskill = 2\n\n[[agents]]\nname = "frost"\nskill = 3\n' > duel.toml && clear`
Enter
Show
Type "duel --config duel.toml duplicate --boards-min 6 --boards-max 6"
Enter
Wait+Screen@120s /verified/
Sleep 3s
