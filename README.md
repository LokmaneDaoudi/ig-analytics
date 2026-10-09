# Instagram Analytics Dashboard (free)

`Instagram API → GitHub Actions (every 2h) → CSVs in /data → static dashboard on GitHub Pages`

## Preview with demo data
```bash
python collect.py --demo
python -m http.server 8765   # open http://localhost:8765
```
**Delete `data/*.csv` before the first real run** so demo rows don't mix with real ones.

## Go live
1. **Instagram account** must be Creator or Business (Settings → Account type).
2. **Meta app:** developers.facebook.com → create app → add the *Instagram API* product → choose
   *API setup with Instagram login*. Add your Instagram account as an Instagram tester and accept the
   invite in the Instagram app (Settings → Website permissions → Tester invites).
3. **Token:** in the app's Instagram API setup, generate a token for your account with the
   `instagram_business_basic` and `instagram_business_manage_insights` permissions. Exchange it for a
   long-lived token if the page offers it (valid 60 days).
4. **GitHub:** create a repo, push this folder, then Settings → Secrets and variables → Actions →
   new secret `IG_TOKEN` = your token.
5. **Pages:** Settings → Pages → deploy from branch `main`, folder `/ (root)`.
6. **First run:** Actions tab → *Collect Instagram analytics* → Run workflow. Then open
   `https://<username>.github.io/<repo>/`.

## Keeping it alive
- Tokens last 60 days. Either set a reminder, or add a `GH_PAT` secret (fine-grained PAT with
  *Secrets: read/write* on this repo) and the workflow refreshes the token every Monday.
- The dashboard shows a banner if data is more than 6 hours old.
- GitHub emails you when a workflow run fails.

## Notes
- The repo (and so the CSVs and dashboard) is public on free GitHub Pages.
- Metric names come from Meta's API and change over time. If a metric starts failing, the script logs a
  warning and leaves that column blank instead of stopping. Check `API_VERSION` and metric lists in
  `collect.py`.
- The collector can't be tested against the real API until you have a token. Only the dashboard and
  demo mode have been tested.

## Brand (Atlas House Brand Book 2.1)
- Colours and the type scale follow pages 21-26 of the book. The dashboard is a single dark surface: black ground, Peach text, Sirius gold for data and links (the book specifies Night for dark grounds; black was chosen instead).
- **Logo:** the header uses the compact mark, reverse version at the Regular weight (book p.18, Peach on Night): `assets/compact-reverse.svg`. Never redraw the mark or the star.
- **Benzin:** the page uses Unbounded as a stand-in. The brand book lists the Benzin web licence as an open decision, so do not add the font files to this public repo until it is confirmed. Then add an `@font-face` for "Benzin" and it replaces the stand-in.

## Follows and unfollows (raw columns)
`data/account_daily.csv` now carries `follower_count`, `fu_total`, `fu_follower`, `fu_non_follower` and `fu_unknown`. These are Instagram's raw follows-and-unfollows numbers. Meta's docs do not say how the follow_type split maps to follows versus unfollows, so the dashboard shows net followers per day (certain) and the gained/lost labels wait until the real values have been checked.
