# Share proposals with OSPool

How an admin sends OSPool the Ganymede projects they choose. OSPool approves every user and every project after
Patchwork's own review, and only US residents may use its capacity, so each project on the page says whether its
whole team is verified US residents in Weave. The rules are in [SPEC.md](../SPEC.md) "Funder packet".

## Make the PDF

1. Open **Admin**, then **Share with OSPool** (`/admin/funders/ospool`).
2. The page lists every project and proposal, newest first, with its status, the amounts requested and granted, and
   its team's US residency. Nothing is ticked.
3. Tick the projects to send. **Open** shows a project's full page.
4. Choose **Download PDF**.
5. Send the file to OSPool yourself. Krater doesn't send it, and OSPool needs no Krater account.

Each download asks Weave again for the teams' names and residency. If Weave can't be reached, Krater makes no file
and says so; try again in a minute. Each download is recorded in the audit log with your Patchwork ID and the
projects in it.

## What is in it

For each ticked project: its title, its one-sentence summary, how the team will use the compute, its status, the
amount requested and granted, the GPU estimate from the application, the team's names and size, and whether every
team member is a verified US resident. Team members under 18 are named as the gallery setting on the admin page says
(full name, first name, or not named).

Never in it: emails, Patchwork IDs, demographics answers (Weave keeps those sealed), the experience and affiliation
answers, the write-up, project secrets or compute logs. Weave doesn't give Krater a member's country, so the PDF says
only whether every member is a verified US resident.

## A team isn't OSPool-ready

The project page tells reviewers and admins who on the team isn't a verified US resident (the "Not OSPool-ready"
line). Each member does the residency check on Weave's verification page. You can still send the project; the PDF
says its team isn't all verified US residents.
