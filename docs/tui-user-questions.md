# Questions while Alysis works

Alysis can ask a structured question while it continues independent work. The
question card supports single choice, multiple choice and a custom answer.
A recommended answer is advisory; it is never submitted automatically.

The agent normally proceeds when the request and available context are enough.
Questions are useful when a missing fact or preference would change the result.
An answer does not change execution permissions or replace an approval dialog.

## Answering a question

Choose an option or type your own answer, then confirm it. In the TUI, Escape
hides the card and Ctrl+G reopens it before expiry. Independent work can continue
while a question is open.

The card has a 60-second inactivity countdown. Typing, editing or using its
controls renews that interval. After expiry, confirmed answers are retained;
an unfinished selection is not submitted. The agent can continue with a stated
assumption when the missing answer is optional. A timeout cannot grant permission
or supply essential information.

Alysis rejects duplicate decisions in one batch and remembers earlier answers.
New information can still require a follow-up question. To try the card, ask for
a preference-sensitive design decision or a short planning interview.
