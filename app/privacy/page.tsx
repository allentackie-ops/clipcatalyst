// /privacy — one of the two stable URLs the platform applications quote
// (PUBLISH.md Part 1). Every factual claim below was checked against the code
// that implements it; where the code and a nicer sentence disagreed, the
// sentence changed.

import type { Metadata } from "next";
import DocPage, {
  DocLink,
  Em,
  List,
  MailLink,
  Note,
  P,
  Sub,
  type DocSection,
} from "@/components/legal/Doc";

export const metadata: Metadata = {
  title: "Privacy Policy — ClipCatalyst",
  description:
    "What ClipCatalyst does with your video, your clips and your account — what never leaves your device, what is deleted when, and what we never do.",
};

const SECTIONS: DocSection[] = [
  {
    id: "summary",
    title: "The short version",
    body: (
      <>
        <P>
          ClipCatalyst is a video tool, and video is personal. This page is the
          long answer; here is the short one.
        </P>
        <List
          items={[
            <>
              The <Em>browser Studio</Em> processes your video on your own
              machine. It is not uploaded, and nothing reports back on what you
              made.
            </>,
            <>
              The <Em>cloud engine</Em> needs your file. It deletes the uploaded
              source as soon as the job finishes — whether the job succeeded or
              failed.
            </>,
            <>
              Clips saved to your library are kept for as long as your plan says
              — 7, 30 or 90 days, or indefinitely. After that the video file is
              deleted and only the clip&apos;s details stay, so your library
              still lists what you made.
            </>,
            <>
              <Em>Nothing you upload trains anything.</Em> There is no training
              pipeline in this product.
            </>,
            <>
              No analytics, no advertising trackers, no third-party scripts, and
              no cookies.
            </>,
            <>
              Card details never reach us. Stripe handles payment; we store a
              customer id and a plan.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "two-engines",
    title: "Two engines, two different answers",
    body: (
      <>
        <P>
          ClipCatalyst can run in two places, and the privacy answer is
          completely different in each. The app tells you which one you are
          using before it starts.
        </P>

        <Sub>In your browser (Studio)</Sub>
        <P>
          Decoding, transcription, speaker detection, face tracking, reframing,
          captioning and the final render all happen inside the page, on your
          own device. Your video is never sent to us, and there is no telemetry
          on the run: no event is recorded when you start one, finish one, or
          abandon one.
        </P>
        <P>
          Two things are downloaded to make that possible — downloaded, never
          uploaded. The speech-recognition model weights are fetched from the
          Hugging Face model hub the first time you transcribe, and your browser
          caches them afterwards; that request carries your IP address, the way
          any file download does, and carries nothing about your video. The
          face-detector weights are served from this site.
        </P>
        <Note>
          The only way a clip your browser rendered ever leaves your device is
          the &ldquo;Save to library&rdquo; button. It is a click. Nothing fires
          on its own when a render finishes, and there is no pre-ticked box
          anywhere near it.
        </Note>
        <P>
          Pressing it uploads that one finished clip, plus its details — title,
          score, hooks, the reason and tip, its timings and dimensions, and the
          clip&apos;s transcript — to your account. Nothing else goes with it,
          and the source video you started from never does.
        </P>

        <Sub>On our servers (the cloud engine)</Sub>
        <P>
          Here, uploading the source is the point. Your file is stored, a worker
          reads it, transcribes it, finds the moments, tracks the speaker and
          renders the clips, and the source file is deleted the moment the job
          reaches a terminal state — success or failure, every exit path.
        </P>
      </>
    ),
  },
  {
    id: "cloud-jobs",
    title: "What a cloud job keeps, and for how long",
    body: (
      <>
        <P>
          While a cloud job runs, a job record holds: an id, the filename you
          uploaded and its size, the settings you asked for (clip length, how
          many clips, output height), the current stage and progress, and a
          user-facing error if it failed. It does not hold your transcript.
        </P>
        <List
          items={[
            <>
              <Em>The uploaded source</Em> is deleted as soon as the job
              finishes, win or lose. A job that is abandoned — a file uploaded
              and never started, a worker that died — has its source removed by
              the maintenance sweep instead.
            </>,
            <>
              <Em>The job record and the clips attached to it</Em> are deleted 48
              hours after the job was created. Download links built on a job stop
              working then; clips saved to your library are a separate thing with
              a separate lifetime, and that sweep never touches them.
            </>,
          ]}
        />
        <P>
          Files are kept on the servers that run ClipCatalyst, and the backend
          can be configured to keep them in an S3 bucket we control instead.
          Where S3 is used, a clip is handed to your browser as a short-lived
          signed link rather than a public URL.
        </P>
      </>
    ),
  },
  {
    id: "library",
    title: "Your library, and how long clips are kept",
    body: (
      <>
        <P>
          A clip you save — whether your browser rendered it or ours did — gets a
          row in your library and a deadline taken from your plan at that moment:
        </P>
        <List
          items={[
            <>
              Free — <Em>7 days</Em>. Starter — <Em>30 days</Em>. Pro —{" "}
              <Em>90 days</Em>. Enterprise — kept as long as the account is.
            </>,
            <>
              At the deadline <Em>the video file is deleted and the record
              stays</Em>: the title, virality score, hooks, the reason and tip,
              the timings, the dimensions and the clip&apos;s transcript. Your
              library still lists what you made and what it said; the video is
              simply no longer downloadable.
            </>,
            <>
              Deleting a clip yourself removes <Em>both</Em> the file and the
              record, and it is the only thing that ever removes a record.
              Nothing on a timer does.
            </>,
            <>
              Upgrading extends the deadline on clips you already saved.
              Downgrading never shortens one — a shorter window only applies to
              clips saved after the change.
            </>,
          ]}
        />
        <P>
          Each account also has a ceiling on the total size of its stored clip
          files. Saving a clip your own browser rendered costs no monthly clip
          allowance, because nothing was rendered on our hardware; it counts
          against that storage ceiling instead.
        </P>
      </>
    ),
  },
  {
    id: "account",
    title: "What an account stores",
    body: (
      <>
        <P>An account record holds exactly this:</P>
        <List
          items={[
            <>your email address, and when the account was created;</>,
            <>
              a <Em>scrypt hash</Em> of your password — or nothing at all, for
              accounts created by Google sign-in or by an emailed code, which
              have no password;
            </>,
            <>
              your Google account id, if you have signed in with Google (see{" "}
              <DocLink href="#google">section 07</DocLink>);
            </>,
            <>
              your Stripe customer id, plan name, subscription status and the end
              of the current paid period (see{" "}
              <DocLink href="#payments">section 05</DocLink>);
            </>,
            <>
              your brand kit: a caption colour, and the logo file itself if you
              uploaded one on a plan that includes it.
            </>,
          ]}
        />
        <Sub>Sessions and sign-in codes</Sub>
        <P>
          Signing in mints an opaque token. We store only its{" "}
          <Em>SHA-256 hash</Em> — the token itself lives in your browser and
          nowhere on our side, so a copy of our database cannot be used to
          impersonate you. Sessions expire 30 days after they are created, and
          signing out revokes one immediately.
        </P>
        <P>
          A six-digit sign-in code is also stored only as a hash, bound to the
          address it was sent to. One code is valid for ten minutes, works once,
          accepts five wrong guesses before it is destroyed, and one address can
          be sent five codes an hour.
        </P>
      </>
    ),
  },
  {
    id: "payments",
    title: "Payments",
    body: (
      <>
        <P>
          Payment is handled by <Em>Stripe</Em>. Checkout and the billing portal
          are pages Stripe hosts — your card details are entered there and never
          touch our servers, in any form.
        </P>
        <List
          items={[
            <>
              When you first start a checkout we create a Stripe customer for
              you. Stripe receives your email address and our internal account id
              so the two records can be matched later.
            </>,
            <>
              We store, from Stripe: the customer id, the plan name, the
              subscription status, and when the current paid period ends. That is
              the whole list.
            </>,
            <>
              Your plan only ever changes from a Stripe event whose signature we
              have verified. Nothing a browser sends us can change what you are
              entitled to.
            </>,
          ]}
        />
        <P>
          Stripe processes that data as its own controller under its privacy
          policy.
        </P>
      </>
    ),
  },
  {
    id: "email",
    title: "Email",
    body: (
      <>
        <P>
          ClipCatalyst sends one kind of email: your six-digit sign-in code.
          There is no newsletter, no product mail and no re-engagement mail in
          the product — there is no code that could send one.
        </P>
        <P>
          Delivery is by <Em>Resend</Em>, which receives the sender line, your
          address, the subject and the plain-text body of that message, and
          nothing else. The code appears in the subject line deliberately, so it
          can be read off a phone notification without opening anything.
        </P>
      </>
    ),
  },
  {
    id: "google",
    title: "Sign in with Google",
    body: (
      <>
        <P>
          If you use Sign in with Google, Google gives your browser an identity
          token and your browser sends it to us. We verify its signature against
          Google&apos;s published public keys — fetching those keys is the only
          request we make to Google, and your token is never sent anywhere.
        </P>
        <P>
          From a verified token we read exactly two values:{" "}
          <Em>your Google account id</Em> and <Em>your email address</Em>. Not
          your name, not your profile photo, and no access to anything in your
          Google account. Google will only be accepted as proof of an address it
          has itself verified.
        </P>
      </>
    ),
  },
  {
    id: "connections",
    title: "Connecting a social account",
    body: (
      <>
        <Note>
          Today, nothing. There is no way to connect a YouTube, TikTok or
          Instagram account to ClipCatalyst — no connect flow, no screen for it,
          and no part of the product that can post on your behalf.
        </Note>
        <P>
          The way to get a clip onto a platform right now is to download it, or
          to use your device&apos;s own share sheet — which hands the file to the
          app you pick, without us being involved.
        </P>
        <P>
          When direct publishing does ship, the design on record is: access and
          refresh tokens encrypted at rest, used only to upload the clips you
          explicitly choose to post, revoked and deleted when you disconnect, and
          never returned by any part of the API. This page will describe it in
          the present tense — with what is actually stored — before the feature
          is switched on.
        </P>
      </>
    ),
  },
  {
    id: "device",
    title: "What lives on your device",
    body: (
      <>
        <List
          items={[
            <>
              <Em>Local storage</Em> holds your session token under{" "}
              <code className="font-mono text-[0.85em] text-zinc-200">
                cc_session
              </code>
              . Clearing your browser storage signs you out.
            </>,
            <>
              <Em>IndexedDB</Em> holds your Studio brand kit — the logo image and
              caption colour — so it survives a reload. Browser renders read it
              from there. If you are signed in on a plan that includes the brand
              kit, saving it also uploads it to your account, so cloud renders
              can use the same kit.
            </>,
            <>
              <Em>No cookies.</Em> The session is a bearer token, not a cookie,
              and the site loads no third-party scripts that could set one.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "logs",
    title: "Rate limits and logs",
    body: (
      <>
        <P>
          Sign-in, registration and checkout are rate-limited, which means we
          count attempts. The counters are keyed on the caller&apos;s IP address
          and — for emailed codes — on the address the code was requested for, so
          nobody can be mail-bombed by a stranger typing their address. They live
          in Redis, expire on their own after a minute (or an hour for the
          per-address limit), and are counters rather than a record of what you
          did.
        </P>
        <P>
          Application logs record job and clip identifiers and the reason
          something failed. They do not contain your video, your audio, your
          transcript, your password or a sign-in code.
        </P>
      </>
    ),
  },
  {
    id: "never",
    title: "What we do not do",
    body: (
      <>
        <List
          items={[
            <>
              <Em>No training.</Em> No video, audio, transcript or clip of yours
              is used to train or fine-tune any model, ours or anybody
              else&apos;s. There is no training pipeline in this product, and the
              virality score is a fixed algorithm reading audio features and the
              transcript — not a model that learns from what you upload.
            </>,
            <>
              <Em>No analytics.</Em> There is no product analytics, no
              advertising or tracking pixel, and no session recording on this
              site today. If that ever changes, this page changes first.
            </>,
            <>
              <Em>No selling or sharing.</Em> Your video, your clips and your
              account details are not sold, rented, or handed to anybody for
              their own purposes. The only third parties involved at all are the
              ones named above — Stripe for payment, Resend for the sign-in
              email, Google if you choose to sign in with it, and the model host
              your browser downloads weights from.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "choices",
    title: "Your choices",
    body: (
      <>
        <List
          items={[
            <>
              <Em>Delete a clip</Em> — any clip in your library, at any time.
              The file and the record both go.
            </>,
            <>
              <Em>Clear your brand kit</Em> — the panel&apos;s clear button
              removes the stored colour and deletes the logo file. It is not
              gated on your plan: taking your own logo off our servers must never
              depend on what you are paying today.
            </>,
            <>
              <Em>Sign out</Em> — revokes that session on our side immediately.
            </>,
            <>
              <Em>Delete your account, or ask for a copy of what we hold</Em> —
              write to <MailLink />. Deletion removes the account record, its
              sessions, its library records and every stored file behind them.
              There is no self-serve button for this yet; when there is, this
              page will say so.
            </>,
          ]}
        />
        <P>
          Depending on where you live you may also have statutory rights of
          access, correction, portability, erasure and objection. The address
          above is where to exercise them.
        </P>
      </>
    ),
  },
  {
    id: "changes",
    title: "Changes, and how to reach us",
    body: (
      <>
        <P>
          The rule we hold ourselves to is that this page describes the software
          as it is built. If the code changes, this page changes with it, and the
          date at the top moves. Substantive changes to what we collect or how
          long we keep it will be described here rather than folded in quietly.
        </P>
        <P>
          Questions, deletion requests, or a claim that something on this page is
          wrong: <MailLink />. If you think a statement here does not match what
          the product actually does, say so — that is a defect, and it will be
          fixed.
        </P>
        <P>
          See also the{" "}
          <DocLink href="/terms">Terms of Service</DocLink>, which cover what the
          service does, what you may upload, and how plans and cancellation work.
        </P>
      </>
    ),
  },
];

export default function PrivacyPage() {
  return (
    <DocPage
      eyebrow="Privacy"
      title="Privacy Policy"
      lede="What happens to your video, your clips and your account — which parts of ClipCatalyst never send your footage anywhere, what the parts that do send it keep, and for how long. It describes the product as it is built today."
      sections={SECTIONS}
    />
  );
}
