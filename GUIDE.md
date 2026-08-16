<!--
Editable source for the in-app guide page (/guide).

The website renders lensinspect/templates/guide.html, NOT this file. Edit here,
then ask Claude to port the changes back into the template — the template adds
markup this file has no way to express (the .callout box, the .kbd key caps, the
numbered .step blocks, and the sign-in-aware button at the end).
-->

# How to use Lens Inspector

*(lede)* You will be shown pictures of galaxies, one at a time, and asked whether each one is a **gravitational lens**. This page tells you what that means and what to press. It takes about two minutes.

## What you are looking for

When a heavy galaxy sits directly in front of a more distant one, its gravity bends the light coming from behind it. The far galaxy stops looking like a blob and gets smeared into a **curved arc, or a full ring, wrapped around the near one**. That is a gravitational lens, and it is rare and valuable.

> **[callout]**
> **The whole job in one sentence.** Look for a curved streak of a *different color* — usually blue or violet — bending around a rounder, brighter galaxy in the middle.

The objects you are shown were picked out by our model as the ones most likely to be lenses, but it gets it wrong often. Your job is to check its guesses. **Most of what you see will not be a lens** — that is expected, and is itself a useful answer. What we are after are the few real lenses hiding among them.

Things that fool people, and are *not* lenses. These are all real objects from this survey:

<!-- COUNTEREXAMPLES — edit the four captions below, then ask Claude to sync them
     to the website. Each one is shown next to its picture at /guide. The bold bit
     is the heading under the image; the rest is the caption. The image files are
     lensinspect/static/img/guide/nonlens-{pair,spiral,trail,elongated}.png and can
     be swapped too if a better example turns up. -->

- A standalone yellow galaxy with fuzzy clumps on the edge. The clumps do not form resolved arcs that resemble a lensed galaxy.
  <!-- image: nonlens-pair.png -->
- A spiral galaxy with two distinct arms spiralling out from the core. They do not resemble the tangential arcs that lensed galaxies form, and they have the same color as the core.
  <!-- image: nonlens-spiral.png -->
- A streak-shaped artifact, possibly from cosmic rays, accidentally identified as an astrophysical source.
  <!-- image: nonlens-trail.png -->
- A grand-design spiral galaxy with two faint blue arms. The arms extend from the core and are also too fuzzy to be a lensed galaxy.
  <!-- image: nonlens-elongated.png -->

## The four answers

<!-- These four definitions come from lensinspect/db.py:GRADE_DESCRIPTIONS and are
     kept word-for-word. Edit them there, not here, if they need to change. -->

| Key | Meaning |
|---|---|
| `1` | **A** — A sure lens - shows clear lensing features and no additional information is needed. |
| `2` | **B** — A probable lens - it shows lensing features but additional information is required to verify it as a definite lens. |
| `3` | **C** — A possible lens - it shows lensing features, but they can be explained without resorting to gravitational lensing. |
| `4` | **X** — Not a lens: Definitively not a lens. |
| `s` | **Not sure — decide later.** Parks it for another look, so you can come back to it. |

Click the buttons or press the keys — they do the same thing. `←` goes back if you want to change an answer.

## What each grade looks like

*(rendered on the website as four scrollable rows of 12 cutouts, one row per grade —
images live in `lensinspect/static/img/guide/examples/{A,B,C,X}/01..12.png`)*

Twelve real examples of each. The **A**, **B** and **C** rows are objects graded by
experts in the published lens catalogues; the **X** row is drawn from this candidate
list, so it shows the kind of thing the model gets wrong. Scroll through them before
you start — the boundary between B and C is where people disagree most, and seeing a
dozen of each is worth more than any description.

## How a session goes

*(rendered as numbered step blocks)*

### 1. Try the practice group first

On the group list there is a group called **Practice**. It has eight real examples, and after each one you see how an expert graded it. Nothing in it counts. Start there.

### 2. Then pick a real group and work down it

Grade each object as it comes. If you are not sure about one, press `s` and move on — that parks it for a second look, it does not answer it.

You cannot miss anything: the first pass hands you every object you have not answered yet, in order, and picks up where you stopped if you close the tab.

**The unsure ones live on the summary page.** Go to **My summary** and click the **still unsure** count — it gives you exactly those objects, then drops you back at the summary with the numbers updated. You can do that as often as you like.

**Unsure is a parking space, not a grade.** An object left unsure is not counted as an answer, so please come back and give it one — the **still unsure** count on your summary shows how many are waiting.

If you come back to one and still cannot decide, that is what **C — borderline** is for. Use it. C is a real answer that says "there is something here, but an ordinary explanation fits just as well", and several people disagreeing on the borderline cases is useful information.

Use the sliders under the picture if they help.

### 3. That is it — there is nothing to submit

Every grade is saved and counted the moment you press the key. There is no Submit button and no finishing line: if you get through 300 objects, that is 300 real grades, and stopping does not throw them away.

Come back whenever you like and carry on where you stopped. You can also change an earlier answer at any time — nothing is ever locked.

## How the list is shared out

The cutouts come to you in a **random order**, and everybody gets a different one. You are not working through the same sequence as the person next to you.

Once a cutout has been graded by enough people — **10** of you on the Q1 list — it **retires**. It is finished, and nobody is shown it again.

> **[callout]**
> **So the progress bar can move without you.** It counts the cutouts that have *not* retired yet, across everyone. Other people are grading at the same time, and every cutout they finish is one fewer left — so the bar creeps forward even during a session where you have graded nothing yourself. That is working as intended, not a glitch.

## Worth knowing

- **You cannot break anything.** Every answer saves as you go. Close the tab whenever; you will pick up where you left off.
- **Guessing wrong is fine.** Several people grade the same objects independently, and the disagreements are informative. Do not try to match what you think others would say.
- **You will not see other people's answers** — that would bias yours.
- **If an image will not load,** mark it unsure and ask Ziqi on Slack.

## Common questions

**Is my work saved? Is there a save button?**
There is no save button — each answer is sent the instant you press the key. If one ever fails to save, a red message appears naming the object and that answer is undone, so a problem is always visible. No red message means it is saved.

**Can I stop and come back later?**
Yes. Close everything and return whenever; you carry on where you stopped. You stay signed in for 60 days.

**Do I have to do the whole group in one sitting?**
No. Do as many as you like; it remembers. Everyone is shown every object, so there is no need to coordinate who takes which.

**Do I see the same objects as everyone else?**
Yes, the same objects in the same order.

**Do I need to submit anything?**
No. Every grade is saved as you go and counts immediately. Stop whenever you like.

<!--
End of page. The template then shows one button, depending on sign-in state:
  signed in  -> "Go to the groups"   (links to the group list)
  signed out -> "Sign in and start"  (links to the login page)
-->
