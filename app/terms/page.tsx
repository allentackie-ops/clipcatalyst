// /terms — the second stable URL the platform applications quote (PUBLISH.md
// Part 1). Everything said here about what the product does, what a plan
// entitles you to and what happens to your files was checked against the code;
// the commercial terms (refunds, notice, liability) are policy, and are written
// to be read rather than to be impressive.

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
  title: "Terms of Service | ClipCatalyst",
  description:
    "The agreement between you and ClipCatalyst: what the service does, what you may upload, how plans and cancellation work, and what we do and do not promise.",
};

const SECTIONS: DocSection[] = [
  {
    id: "agreement",
    title: "This agreement",
    body: (
      <>
        <P>
          These terms are the agreement between you and ClipCatalyst
          (&ldquo;we&rdquo;, &ldquo;us&rdquo;). Using the site, the Studio or the
          cloud engine means you accept them. The{" "}
          <DocLink href="/privacy">Privacy Policy</DocLink> is part of them and
          describes what happens to your video and your data.
        </P>
        <P>
          They are written in plain English on purpose. Where a sentence here
          describes what the software does, it describes what it actually does.
          If you find one that does not, tell us at <MailLink /> and it will be
          corrected.
        </P>
      </>
    ),
  },
  {
    id: "service",
    title: "What ClipCatalyst does",
    body: (
      <>
        <P>
          ClipCatalyst takes a long video, finds the moments worth posting, cuts
          them to 9:16, keeps the speaker in frame, burns in captions and scores
          each clip from 0 to 100 with a reason and a suggestion. There are two
          ways to run it:
        </P>
        <List
          items={[
            <>
              <Em>Studio, in your browser.</Em> Everything runs on your own
              device; no account is needed and your video is not uploaded.
              Studio is in beta.
            </>,
            <>
              <Em>The cloud engine.</Em> Your file is uploaded and rendered on
              our hardware. It needs an account, and it spends your monthly clip
              allowance.
            </>,
          ]}
        />
        <P>
          You can also connect a YouTube channel so clips you choose can be
          posted to it. A post made through ClipCatalyst is still your post:
          YouTube&apos;s own terms apply to it, you are responsible for what goes
          up, and nothing is ever posted without you asking for it. What we store
          for a connection, and how to revoke it, is in the{" "}
          <DocLink href="/privacy#connections">Privacy Policy</DocLink>.
        </P>
        <P>
          Features described on our marketing pages are not all available yet.
          What the product does when you use it is what you are buying; nothing
          on a marketing page is a term of this agreement.
        </P>
      </>
    ),
  },
  {
    id: "account",
    title: "Your account",
    body: (
      <>
        <List
          items={[
            <>
              You can sign in with a password, with a Google account, or with a
              six-digit code emailed to you. Any of them signs into the same
              account for a given address.
            </>,
            <>
              Keep your credentials to yourself. Anything done with a live
              session is treated as done by you. If you think a session is not
              yours, sign out (that revokes it) and write to us.
            </>,
            <>
              You must be old enough to enter into a contract where you live, and
              at least 13, to hold an account. ClipCatalyst is not directed at
              children.
            </>,
            <>
              One person or organisation per account. Sharing credentials to
              share an allowance is not what a plan buys.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "your-content",
    title: "Your content stays yours",
    body: (
      <>
        <Note>
          You own the video you bring and the clips ClipCatalyst makes from it.
          We claim no ownership of either, and no right to publish, display or
          resell your footage.
        </Note>
        <P>
          The only licence you give us is the narrow one the service needs to
          function: permission to store and process your file so the job can
          produce the clips you asked for, and to store the clips you choose to
          save so we can hand them back to you. It lasts as long as we hold the
          file, which for an uploaded source is until the job finishes.
        </P>
        <P>
          We do not use your video, audio, transcripts or clips to train or
          fine-tune any model. See{" "}
          <DocLink href="/privacy#never">
            Privacy: what we do not do
          </DocLink>
          .
        </P>
      </>
    ),
  },
  {
    id: "acceptable-use",
    title: "What you may not upload or do",
    body: (
      <>
        <P>
          By uploading something you are asserting that you have the rights to
          it, and to the clips made from it. That is the main rule, and most of
          the rest follows from it.
        </P>
        <List
          items={[
            <>
              <Em>No content you do not have the rights to.</Em> Somebody
              else&apos;s film, show, stream, music or footage, uploaded so the
              engine can cut it up for you, is exactly the case this rule exists
              for.
            </>,
            <>
              <Em>Nothing unlawful</Em>: material that infringes copyright or
              trademark, invades someone&apos;s privacy, is defamatory, depicts
              the sexual abuse of children, or that you are legally barred from
              processing.
            </>,
            <>
              <Em>No attacks on the service</Em>: probing for vulnerabilities,
              scripted abuse, working around quotas, stripping or defeating the
              watermark on a free-tier export, or reselling access.
            </>,
          ]}
        />
        <P>
          We do not review clips before you post them, and we do not watch what
          you upload. But we may suspend an account, or remove content, when we
          become aware of a breach of this section, and where we can, we will
          say why.
        </P>
      </>
    ),
  },
  {
    id: "plans",
    title: "Plans, allowances and the watermark",
    body: (
      <>
        <Note>
          Free-tier exports are watermarked. Every clip a free account renders
          carries the ClipCatalyst mark, in the browser and in the cloud alike,
          and paid plans remove it.
        </Note>
        <P>
          The plans, as the software enforces them. Free: 3 clips a month, up to
          720p, watermarked. Starter: 30 clips, up to 1080p, no watermark, brand
          kit. Pro: 100 clips, up to 4K. Enterprise: unlimited clips. Current
          prices are on the{" "}
          <DocLink href="/#pricing">pricing page</DocLink>.
        </P>
        <List
          items={[
            <>
              The allowance is per <Em>calendar month</Em> (UTC) and resets on
              the 1st. Unused clips do not roll over.
            </>,
            <>
              <Em>A clip that fails to render costs nothing.</Em> A job reserves
              its clips when it starts and gives back whatever did not render,
              including everything, if the job failed outright.
            </>,
            <>
              Saving a clip your own browser rendered does not spend the
              allowance, because nothing was rendered on our hardware. It counts
              against your account&apos;s stored-clip ceiling instead.
            </>,
            <>
              What a render is allowed to do (resolution, watermark, brand kit)
              is read from your plan <Em>at the moment the render starts</Em>. An
              upgrade applies to your next render; a plan that has lapsed stops
              applying to it.
            </>,
            <>
              Clip retention follows your plan (7 / 30 / 90 days, or
              indefinitely). Upgrading extends clips you already saved;
              downgrading never shortens them. See{" "}
              <DocLink href="/privacy#library">
                Privacy: your library
              </DocLink>
              .
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "speed",
    title: "About the 90 seconds",
    body: (
      <>
        <Note>
          90 seconds is a <Em>target</Em>, not a guarantee. It is not a term of
          this agreement, and nothing is owed if a particular job takes longer.
        </Note>
        <P>
          The figure on our marketing pages is a target median for the cloud
          pipeline on representative footage, and it is labelled as one there
          too. What a real job takes depends on the length, format and bitrate of
          your source, how many clips you asked for, the output resolution, and
          how busy the queue is when you start.
        </P>
        <P>
          A cloud job that runs past the server&apos;s render timeout is stopped
          and reported as failed, and it returns the allowance it reserved, so
          you are not charged clips for a run that did not deliver them.
        </P>
        <P>
          Runs in the browser depend entirely on your own machine. Without GPU
          acceleration they are considerably slower, and the app says so while
          it works rather than pretending otherwise.
        </P>
      </>
    ),
  },
  {
    id: "billing",
    title: "Payment, cancellation and refunds",
    body: (
      <>
        <Sub>Billing</Sub>
        <P>
          Paid plans are monthly subscriptions billed through Stripe, charged in
          advance for each period. Card details are entered on Stripe&apos;s own
          pages and never reach us.
        </P>
        <Sub>Cancelling</Sub>
        <P>
          Cancel at any time from <Em>Manage billing</Em> on your account page,
          which opens Stripe&apos;s billing portal. Cancelling stops the next
          charge; your plan&apos;s entitlements last until the end of the period
          you have already paid for, after which the account returns to Free.
          Clips already in your library keep the retention deadline they were
          given, because a downgrade does not shorten it.
        </P>
        <Sub>Failed payments</Sub>
        <P>
          If a renewal charge fails, Stripe retries it. Your plan keeps working
          during that window, up to fourteen days past the end of the paid
          period, and then the account drops to Free until payment succeeds.
        </P>
        <Sub>Refunds</Sub>
        <P>
          We do not automatically refund part-used months; cancelling leaves you
          with the time you paid for. If the service failed you, whether that
          is a run of broken jobs, a charge you did not expect, or a plan that
          did not do what this page says it does, write to <MailLink /> and we
          will sort it out.
          Statutory cancellation and refund rights where you live are unaffected
          by anything on this page.
        </P>
      </>
    ),
  },
  {
    id: "availability",
    title: "Availability and changes to the service",
    body: (
      <>
        <List
          items={[
            <>
              The service is provided on a best-effort basis. There is no uptime
              guarantee, and it can be unavailable for maintenance, an outage, or
              a failure at an upstream provider.
            </>,
            <>
              Beta features, and Studio is one, can change or be withdrawn. We
              will not remove a paid entitlement without notice.
            </>,
            <>
              Plans and prices can change. A price change takes effect from your
              next billing period, never retroactively, and you can cancel before
              it applies.
            </>,
            <>
              These terms can change. The date at the top moves when they do, and
              a change that materially reduces what you get will be notified
              before it takes effect.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "deletion",
    title: "Deletion and ending the agreement",
    body: (
      <>
        <P>You can end this agreement at any time by cancelling and leaving.</P>
        <List
          items={[
            <>
              <Em>You delete</Em>: any clip in your library, file and record
              both; your brand kit, logo file included; a connected channel,
              which is revoked with the platform and then removed here; your
              account, by writing to <MailLink />.
            </>,
            <>
              <Em>We delete automatically</Em>: an uploaded source, as soon as
              its job finishes; a job record and the clips attached to it, 48
              hours after the job was created; the record of a finished post to a
              channel, 48 hours after you asked for it, which leaves the video
              itself on your channel; a saved clip&apos;s video file at the end
              of your plan&apos;s retention window, after which the clip&apos;s
              details stay so your library still lists it.
            </>,
            <>
              <Em>We may terminate</Em> an account for a serious or repeated
              breach of{" "}
              <DocLink href="#acceptable-use">
                what you may not upload or do
              </DocLink>
              , or for any reason on reasonable notice, in which case we refund
              any unused prepaid period.
            </>,
          ]}
        />
      </>
    ),
  },
  {
    id: "liability",
    title: "Warranties and liability",
    body: (
      <>
        <P>
          The service is provided <Em>as is</Em>. Clip selection is automated and
          imperfect: scores are estimates, transcripts contain errors, speaker
          tracking can miss, and a moment the engine loves may be one you would
          never post. Watch anything before you publish it. The judgement is
          yours, and so is the responsibility for what you put out.
        </P>
        <P>
          To the maximum extent the law allows, we are not liable for lost
          profits, lost audience, lost or corrupted data, or any indirect or
          consequential loss; and our total liability for any claim is limited to
          the amount you paid us in the twelve months before it arose, which
          for a free account is nothing.
        </P>
        <P>
          Nothing here limits liability that cannot lawfully be limited,
          including for fraud, or for death or personal injury caused by
          negligence, and nothing here removes consumer rights you have by
          statute.
        </P>
        <P>
          You agree to cover us for claims arising from content you uploaded that
          you did not have the rights to.
        </P>
      </>
    ),
  },
  {
    id: "contact",
    title: "Contact",
    body: (
      <>
        <P>
          Questions about these terms, a billing problem, a deletion request, or
          a claim that something here is inaccurate: <MailLink />.
        </P>
        <P>
          For what happens to your video and your data, see the{" "}
          <DocLink href="/privacy">Privacy Policy</DocLink>.
        </P>
      </>
    ),
  },
];

export default function TermsPage() {
  return (
    <DocPage
      title="Terms of Service"
      lede="The agreement between you and ClipCatalyst: what the service does, what you may upload, how plans, cancellation and the watermark work, and what we do and do not promise."
      sections={SECTIONS}
    />
  );
}
