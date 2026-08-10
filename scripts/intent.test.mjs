// Behavioural tests for the deterministic chat intent parser.
import { parseIntent, DEFAULT_RESIZE_S } from "../.croptrack-build/intent.js";

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`PASS  ${name}`); }
  else { fail++; console.log(`FAIL  ${name} ${extra}`); }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// Active clip: 3 clips in the run, clip 1 active, transcript words in display
// time. " Funnels," and " laughs" exist so word-search phrasings can land.
const ctx = {
  clipCount: 3,
  activeClip: 0,
  duration: 24.5,
  pauses: [{ start: 1.5, end: 2.7, length: 1.2 }],
  hooks: ["Hook one", "Hook two", "Hook three"],
  words: [
    { text: " We", start: 0.6 }, { text: " were", start: 1.0 },
    { text: " talking", start: 2.7 }, { text: " about", start: 3.77 },
    { text: " Funnels,", start: 4.94 }, { text: " and", start: 6.8 },
    { text: " retention", start: 8.0 }, { text: " because", start: 10.0 },
    { text: " he", start: 12.0 }, { text: " laughs", start: 13.2 },
    { text: " loudly", start: 14.0 },
  ],
};
const p = (s, c = ctx) => parseIntent(s, c);
// One-command intents collapse to { cmd, target, note } for terse asserts.
const one = (s, c = ctx) => {
  const i = p(s, c);
  if (i.kind !== "commands" || i.commands.length !== 1) return null;
  return { cmd: i.commands[0], target: i.targetClip, note: i.note };
};

// 1. The three blueprint sentences, verbatim.
{
  const a = one("Make clip 2 more energetic");
  ok("blueprint: energetic command", a && eq(a.cmd, { type: "energetic" }), JSON.stringify(a));
  ok("blueprint: energetic targets clip 2 (0-based 1)", a && a.target === 1);

  const ctx4 = { ...ctx, clipCount: 4 };
  const b = one("Remove the awkward pause in clip 4", ctx4);
  ok("blueprint: awkward pause -> cutPause longest", b && eq(b.cmd, { type: "cutPause", which: "longest" }), JSON.stringify(b));
  ok("blueprint: pause targets clip 4 (0-based 3)", b && b.target === 3);

  const c = one("Add a zoom on the host's face when he laughs");
  ok("blueprint: laugh found -> zoom at the word start", c && eq(c.cmd, { type: "zoom", at: 13.2 }), JSON.stringify(c));

  const noLaugh = { ...ctx, words: ctx.words.filter((w) => !/laugh/i.test(w.text)) };
  const d = p("Add a zoom on the host's face when he laughs", noLaugh);
  ok("blueprint: no laugh word -> honest unknown", d.kind === "unknown", d.kind);
  ok("blueprint: laugh refusal admits the limit", d.kind === "unknown" && /can't detect laughter/.test(d.reply), d.reply);
  ok("blueprint: laugh refusal carries a usable example", d.kind === "unknown" && d.reply.includes('"zoom at 0:12"'), d.reply);
}

// 2. Pauses.
{
  const a = one("cut the pause");
  ok("cut the pause -> longest", a && eq(a.cmd, { type: "cutPause", which: "longest" }));
  ok("no clip named -> active clip", a && a.target === 0);
  ok("active clip 2 is the default target", one("cut the pause", { ...ctx, activeClip: 2 }).target === 2);
  ok("remove the pause at 0:14 -> at 14", eq(one("remove the pause at 0:14").cmd, { type: "cutPause", which: { at: 14 } }));
  ok("cut the pause at 12s -> at 12", eq(one("cut the pause at 12s").cmd, { type: "cutPause", which: { at: 12 } }));
  ok("remove all the pauses -> all", eq(one("remove all the pauses").cmd, { type: "cutPause", which: "all" }));
  ok("cut the dead air -> all", eq(one("cut the dead air").cmd, { type: "cutPause", which: "all" }));
  ok("remove the silences -> all", eq(one("remove the silences").cmd, { type: "cutPause", which: "all" }));
  ok("tighten it up -> tighten", eq(one("tighten it up").cmd, { type: "tighten" }));
  ok("Tighten, please. -> tighten (punctuation)", eq(one("Tighten, please.").cmd, { type: "tighten" }));
}

// 3. Energy.
{
  ok("make it more energetic", eq(one("make it more energetic").cmd, { type: "energetic" }));
  ok("punchier", eq(one("punchier").cmd, { type: "energetic" }));
  ok("give it more energy", eq(one("give it more energy").cmd, { type: "energetic" }));
  ok("faster paced", eq(one("faster paced").cmd, { type: "energetic" }));
  ok("give it more punch", eq(one("give it more punch").cmd, { type: "energetic" }));
}

// 4. Trims + the number/time parsing table.
{
  ok("trim the first 2 seconds", eq(one("trim the first 2 seconds").cmd, { type: "trim", edge: "start", seconds: 2 }));
  ok("cut the first second (bare unit = 1)", eq(one("cut the first second").cmd, { type: "trim", edge: "start", seconds: 1 }));
  ok("cut the first two seconds (word number)", eq(one("cut the first two seconds").cmd, { type: "trim", edge: "start", seconds: 2 }));
  ok("trim the last 3 seconds", eq(one("trim the last 3 seconds").cmd, { type: "trim", edge: "end", seconds: 3 }));
  ok("start 2 seconds later", eq(one("start 2 seconds later").cmd, { type: "trim", edge: "start", seconds: 2 }));
  ok("start half a second earlier", eq(one("start half a second earlier").cmd, { type: "trim", edge: "start", seconds: -0.5 }));
  ok("start a second later ('a' = 1)", eq(one("start a second later").cmd, { type: "trim", edge: "start", seconds: 1 }));
  ok("start five seconds later (word number)", eq(one("start five seconds later").cmd, { type: "trim", edge: "start", seconds: 5 }));
  ok("start 12 seconds later", eq(one("start 12 seconds later").cmd, { type: "trim", edge: "start", seconds: 12 }));
  ok("end 1.5 seconds earlier (float)", eq(one("end 1.5 seconds earlier").cmd, { type: "trim", edge: "end", seconds: 1.5 }));
  ok("extend the end by 2 seconds", eq(one("extend the end by 2 seconds").cmd, { type: "trim", edge: "end", seconds: -2 }));
  ok("make it 2 seconds shorter -> trim end", eq(one("make it 2 seconds shorter").cmd, { type: "trim", edge: "end", seconds: 2 }));
  const longer = one("make it longer");
  ok("make it longer -> extend end by default", longer && eq(longer.cmd, { type: "trim", edge: "end", seconds: -DEFAULT_RESIZE_S }), JSON.stringify(longer));
  ok("make it longer carries an honest note", longer && typeof longer.note === "string" && longer.note.includes(`${DEFAULT_RESIZE_S} seconds`), longer && longer.note);
  ok("DEFAULT_RESIZE_S = 2", DEFAULT_RESIZE_S === 2);
  ok("cut from 0:05 to 0:08 -> explicit cut", eq(one("cut from 0:05 to 0:08").cmd, { type: "cut", start: 5, end: 8 }));
}

// 5. Zooms.
{
  ok("add a zoom at 0:08", eq(one("add a zoom at 0:08").cmd, { type: "zoom", at: 8 }));
  ok("zoom in at 12 seconds", eq(one("zoom in at 12 seconds").cmd, { type: "zoom", at: 12 }));
  ok("zoom at 12 (bare number after at)", eq(one("zoom at 12").cmd, { type: "zoom", at: 12 }));
  ok("Zoom At 0:12. (case/punctuation)", eq(one("Zoom At 0:12.").cmd, { type: "zoom", at: 12 }));
  ok("punch in when he says funnel (prefix match)", eq(one("punch in when he says funnel").cmd, { type: "zoom", at: 4.94 }));
  ok("zoom when she says retention", eq(one("zoom when she says retention").cmd, { type: "zoom", at: 8 }));
  ok("remove the zoom -> clearZooms", eq(one("remove the zoom").cmd, { type: "clearZooms" }));
  ok("remove the zooms -> clearZooms", eq(one("remove the zooms").cmd, { type: "clearZooms" }));
  const where = p("add a zoom");
  ok("zoom with no time is an honest unknown", where.kind === "unknown" && where.reply.includes('"zoom at 0:12"'), JSON.stringify(where));
  const miss = p("zoom in when he says blockchain");
  ok("unspoken word refused by name", miss.kind === "unknown" && miss.reply.includes('"blockchain"'), JSON.stringify(miss));
  ok("unspoken-word refusal carries an example", miss.kind === "unknown" && miss.reply.includes('"zoom at 0:12"'));
}

// 6. Captions.
{
  ok("turn off captions", eq(one("turn off captions").cmd, { type: "captions", on: false }));
  ok("captions off", eq(one("captions off").cmd, { type: "captions", on: false }));
  ok("hide the subtitles", eq(one("hide the subtitles").cmd, { type: "captions", on: false }));
  ok("captions on", eq(one("captions on").cmd, { type: "captions", on: true }));
  ok("bring back captions", eq(one("bring back captions").cmd, { type: "captions", on: true }));
}

// 7. Hooks.
{
  ok("use hook b -> useHook 1", eq(one("use hook b").cmd, { type: "useHook", index: 1 }));
  ok("use the second hook -> useHook 1", eq(one("use the second hook").cmd, { type: "useHook", index: 1 }));
  ok("use hook 3 -> useHook 2", eq(one("use hook 3").cmd, { type: "useHook", index: 2 }));
  ok("try another hook -> cycleHook", eq(one("try another hook").cmd, { type: "cycleHook" }));
  ok("switch hooks -> cycleHook", eq(one("switch hooks").cmd, { type: "cycleHook" }));
}

// 8. Undo / reset.
{
  ok("undo -> undo intent", eq(p("undo"), { kind: "undo" }));
  ok("undo that -> undo intent", eq(p("undo that"), { kind: "undo" }));
  ok("undo the zoom -> clearZooms", eq(one("undo the zoom").cmd, { type: "clearZooms" }));
  ok("reset -> reset command", eq(one("reset").cmd, { type: "reset" }));
  ok("reset the edits", eq(one("reset the edits").cmd, { type: "reset" }));
  ok("start over -> reset, not a start trim", eq(one("start over").cmd, { type: "reset" }));
}

// 9. Targeting edge cases.
{
  const a = one("tighten up the last clip");
  ok("the last clip -> clipCount-1", a && a.target === 2 && eq(a.cmd, { type: "tighten" }), JSON.stringify(a));
  const b = one("make the second clip punchier");
  ok("ordinal clip targeting", b && b.target === 1 && eq(b.cmd, { type: "energetic" }), JSON.stringify(b));
  const c = one("remove the pause in clip #2");
  ok("clip #2 form", c && c.target === 1 && eq(c.cmd, { type: "cutPause", which: "longest" }), JSON.stringify(c));
  const far = p("cut the pause in clip 9");
  ok("clip 9 of 3 -> unknown", far.kind === "unknown", far.kind);
  ok("out-of-range reply names the valid range",
    far.kind === "unknown" && /only 3 clips/.test(far.reply) && far.reply.includes('"clip 1"') && far.reply.includes('"clip 3"'),
    far.reply);
}

// 10. Help.
{
  const h = p("help");
  ok("help intent", h.kind === "help");
  ok("help lists real pause/zoom/trim examples",
    h.kind === "help" &&
      h.reply.includes('"remove the pause at 0:14"') &&
      h.reply.includes('"add a zoom at 0:08"') &&
      h.reply.includes('"trim the first 2 seconds"') &&
      h.reply.includes('"undo"'),
    h.reply);
  ok("what can you do -> help", p("what can you do").kind === "help");
}

// 11. Unknown input never guesses; the reply is actionable.
{
  const u = p("make it rain");
  ok("nonsense -> unknown", u.kind === "unknown");
  ok("unknown reply carries 2-3 usable examples",
    u.kind === "unknown" &&
      u.reply.includes('"remove the pause at 0:14"') &&
      u.reply.includes('"make it more energetic"') &&
      u.reply.includes('"trim the first 2 seconds"'),
    u.reply);
  ok("empty input -> unknown", p("").kind === "unknown");
  ok("greeting -> unknown, no command invented", p("hello there").kind === "unknown");
}

// 12. Determinism: same input, same object, every time.
{
  const inputs = ["Make clip 2 more energetic", "remove the pause at 0:14", "make it longer", "help", "garbage in"];
  let stable = true;
  for (const s of inputs) if (!eq(p(s), p(s))) { stable = false; break; }
  ok("parseIntent is deterministic", stable);
}

// 13. Negation: a negated wish must never execute the negated command.
{
  const a = p("don't remove the pause");
  ok("don't remove the pause -> unknown, nothing cut", a.kind === "unknown", JSON.stringify(a));
  ok("negation reply says left alone + real example",
    a.kind === "unknown" && /left alone/.test(a.reply) && a.reply.includes('"remove the pause at 0:14"'), a.reply);
  const b = p("do not zoom");
  ok("do not zoom -> unknown negation reply", b.kind === "unknown" && /left alone/.test(b.reply), JSON.stringify(b));
  const c = p("never cut anything");
  ok("never cut anything -> unknown negation reply", c.kind === "unknown" && /left alone/.test(c.reply), JSON.stringify(c));
  const d = p("please don't touch the captions");
  ok("please don't touch the captions -> unknown negation reply", d.kind === "unknown" && /left alone/.test(d.reply), JSON.stringify(d));
  // Legitimate commands with no negation still parse.
  ok("negation guard: plain removal still parses", eq(one("remove the pause at 0:14").cmd, { type: "cutPause", which: { at: 14 } }));
  ok("negation guard: 'no captions' still means captions off", eq(one("no captions").cmd, { type: "captions", on: false }));
}

// 14. The bare pause noun is not a removal order.
{
  const a = p("I like the pause at 0:14");
  ok("I like the pause at 0:14 -> unknown, not a cut", a.kind === "unknown", JSON.stringify(a));
  ok("protective-pause reply carries a real example",
    a.kind === "unknown" && a.reply.includes('"remove the pause at 0:14"'), a.reply);
  const b = p("keep the pauses");
  ok("keep the pauses -> unknown, not cutPause all", b.kind === "unknown", JSON.stringify(b));
  const c = p("the silence works well");
  ok("the silence works well -> unknown, not a cut", c.kind === "unknown", JSON.stringify(c));
  const d = p("what about the pause");
  ok("pause noun with no verb -> clarifying unknown", d.kind === "unknown" && d.reply.includes('"remove the pause at 0:14"'), JSON.stringify(d));
}

// 15. Conjoined requests: every parseable clause executes, in order.
{
  const a = p("remove the pause and add a zoom at 0:10");
  ok("pause + zoom conjunction -> both commands, sentence order",
    a.kind === "commands" && a.commands.length === 2 &&
      eq(a.commands[0], { type: "cutPause", which: "longest" }) &&
      eq(a.commands[1], { type: "zoom", at: 10 }),
    JSON.stringify(a));
  ok("conjunction keeps one target clip", a.kind === "commands" && a.targetClip === 0);
  const b = p("tighten it up then captions off");
  ok("tighten then captions off -> both commands",
    b.kind === "commands" && b.commands.length === 2 &&
      eq(b.commands[0], { type: "tighten" }) && eq(b.commands[1], { type: "captions", on: false }),
    JSON.stringify(b));
  const c = p("remove the pause and add a zoom at 0:10 and make it sparkle");
  ok("partially-understood conjunction executes the parsed clauses",
    c.kind === "commands" && c.commands.length === 2 &&
      eq(c.commands[0], { type: "cutPause", which: "longest" }) &&
      eq(c.commands[1], { type: "zoom", at: 10 }),
    JSON.stringify(c));
  ok("...and the note names what was NOT understood",
    c.kind === "commands" && typeof c.note === "string" && c.note.includes('"make it sparkle"'), c.note);
  // The trap: 'and' inside a word-search phrase must NOT be split.
  const salty = { ...ctx, words: [...ctx.words, { text: " salt", start: 7.2 }] };
  const d = p("zoom in when he says salt and pepper", salty);
  ok("word-search 'salt and pepper' stays ONE command",
    d.kind === "commands" && d.commands.length === 1 && eq(d.commands[0], { type: "zoom", at: 7.2 }),
    JSON.stringify(d));
  ok("...with no leftover-clause note", d.kind === "commands" && d.note === undefined, JSON.stringify(d));
}

// 16. Crop/framing words never trigger the pause-cutting tighten.
{
  const a = p("make the framing tighter");
  ok("make the framing tighter -> unknown, not tighten", a.kind === "unknown", JSON.stringify(a));
  ok("crop refusal is honest about the limit",
    a.kind === "unknown" && /can't change the crop or framing yet/.test(a.reply), a.reply);
  const b = p("tighter crop on his face");
  ok("tighter crop on his face -> unknown, not tighten",
    b.kind === "unknown" && /can't change the crop or framing yet/.test(b.reply), JSON.stringify(b));
  ok("plain tighten still works", eq(one("tighten it up").cmd, { type: "tighten" }));
}

// 17. Minutes are minutes, not seconds.
{
  const min2 = one("trim the first 2 minutes");
  ok("trim the first 2 minutes -> 120 s", min2 && eq(min2.cmd, { type: "trim", edge: "start", seconds: 120 }), JSON.stringify(min2));
  const minWord = one("trim the first two minutes");
  ok("trim the first two minutes (word number) -> 120 s", minWord && eq(minWord.cmd, { type: "trim", edge: "start", seconds: 120 }), JSON.stringify(minWord));
  const min1 = one("start 1 minute later");
  ok("start 1 minute later -> 60 s", min1 && eq(min1.cmd, { type: "trim", edge: "start", seconds: 60 }), JSON.stringify(min1));
  ok("mm:ss unchanged: trim the first 1:30 -> 90 s", eq(one("trim the first 1:30").cmd, { type: "trim", edge: "start", seconds: 90 }));
  ok("plain seconds unchanged: trim the first 2 seconds", eq(one("trim the first 2 seconds").cmd, { type: "trim", edge: "start", seconds: 2 }));
}

// 18. Help only swallows bare help-ish messages.
{
  const a = one("help me cut the pause at 0:14");
  ok("help me cut the pause at 0:14 -> the cut, not the help wall",
    a && eq(a.cmd, { type: "cutPause", which: { at: 14 } }), JSON.stringify(a));
  ok("bare help still helps", p("help").kind === "help");
  ok("help? still helps", p("help?").kind === "help");
  ok("what can you do still helps", p("what can you do").kind === "help");
}

// 19. Caption styling is refused honestly; scoped zoom removal admits scope.
{
  for (const s of ["make the captions bigger", "yellow captions", "add captions in a cool font"]) {
    const r = p(s);
    ok(`${s} -> unknown, captions not toggled`,
      r.kind === "unknown" && /I can only turn captions on or off right now/.test(r.reply), JSON.stringify(r));
  }
  ok("captions off still parses", eq(one("captions off").cmd, { type: "captions", on: false }));
  const z = one("remove the zoom at 0:12");
  ok("remove the zoom at 0:12 -> clearZooms (the only removal we have)",
    z && eq(z.cmd, { type: "clearZooms" }), JSON.stringify(z));
  ok("...and the note admits ALL zooms were cleared",
    z && typeof z.note === "string" && /all zooms were cleared/.test(z.note), z && z.note);
}

// 20. Default-amount notes never assert the edit was applied.
{
  const longer = one("make it longer");
  ok("make it longer note says 'going with', not 'I extended'",
    longer && typeof longer.note === "string" && longer.note.includes("going with") &&
      !/\bI (?:extended|trimmed)\b/i.test(longer.note),
    longer && longer.note);
  const shorter = one("make it shorter");
  ok("make it shorter note says 'going with', not 'I trimmed'",
    shorter && typeof shorter.note === "string" && shorter.note.includes("going with") &&
      !/\bI (?:extended|trimmed)\b/i.test(shorter.note),
    shorter && shorter.note);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
