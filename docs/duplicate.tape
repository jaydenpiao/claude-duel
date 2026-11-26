# `duel duplicate` GIF: mirrored boards on the fake provider ($0, no network, no API key).
# 12 boards: under the margin rule too few paired boards can never back a winner (exact sign-flip p >= 1/2^n, and 4 boards give 0.0625).
# Re-render from the repo root: vhs docs/duplicate.tape   (needs vhs and `duel` on PATH; keep it under 2 MB)
Output docs/duplicate.gif
Set Shell bash
Set FontSize 16
Set Width 1100
Set Height 400
Set Framerate 12
Set TypingSpeed 30ms
Hide
Type `export D=$(mktemp -d) && cd $D && unset OPENAI_API_KEY NO_COLOR FORCE_COLOR VIRTUAL_ENV_PROMPT && export DUEL_SANDBOX=subprocess DUEL_MODE=ladder DUEL_LOG_LEVEL=WARNING && PS1='$ ' && printf '[[agents]]\nname = "ember"\nskill = 2\n\n[[agents]]\nname = "frost"\nskill = 3\n' > duel.toml && clear`
Enter
Show
Type "duel --config duel.toml duplicate --boards-min 12 --boards-max 12"
Enter
Sleep 20s
