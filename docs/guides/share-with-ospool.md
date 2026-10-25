# Share proposals with OSPool

How an admin sends OSPool the Ganymede projects it could run. OSPool approves every user and every project after
Patchwork's own review, and only US residents may use its capacity, so the packet lists only projects whose whole
team is a verified US resident in Weave. The rules are in [SPEC.md](../SPEC.md) "Funder packet".

## Make the packet

1. Open **Admin**, then **Share with OSPool** (`/admin/funders/ospool`).
2. The page lists approved projects whose whole team is verified US residents. To include proposals that are still
   waiting for review, choose **Also show proposals waiting for review**.
3. Untick any project you don't want to send.
4. Choose **Download PDF** to send as is, or **Download CSV** for a spreadsheet.
5. Send the file to OSPool yourself. Krater doesn't send it, and OSPool needs no Krater account.

Each download checks residency in Weave again. If Weave can't be reached, Krater makes no file and says so; try again
in a minute. Each download is recorded in the audit log with your Patchwork ID and the projects in it.

## What is in it

For each project: its title, its one-sentence summary, how the team will use the compute, its status, the amount
requested and granted, the GPU estimate from the application, and the team's names and size. Team members under 18
are named as the gallery setting on the admin page says (full name, first name, or not named).

Never in it: emails, Patchwork IDs, demographics answers (Weave keeps those sealed), the experience and affiliation
answers, the write-up, project secrets or compute logs. Weave doesn't give Krater a member's country, so the packet
says only that every member is a verified US resident.

## A project is missing

- **Someone on the team isn't a verified US resident.** The project page tells reviewers and admins who (the
  "Not OSPool-ready" line). Each member does the residency check on Weave's verification page.
- **It isn't approved yet.** Turn on "Also show proposals waiting for review", or approve it first.
- **It's completed or withdrawn.** Those aren't shared: they no longer need compute.
