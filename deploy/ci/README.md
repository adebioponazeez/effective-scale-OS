# CI definition (activation required)

`ci.yml` in this directory is the full pipeline: ontology/docs drift audit, kernel suite, race-style stress, longevity soak, SAF
subproject suite (including the live embedded-kernel worker test), container build with a
boot + health check, and a Go-port gate that activates automatically once `go.mod` lands.

**Why it lives here and not in `.github/workflows/`:** the automation token used to push this
branch is a GitHub App installation **without the `workflows` permission**, and GitHub rejects
any commit that creates or updates `.github/workflows/*` from such a token. The file *does*
exist in the working tree at `.github/workflows/ci.yml` (it is left untracked for exactly this
reason).

To activate, either push from an account/token with `workflows` scope:

```bash
mkdir -p .github/workflows && cp deploy/ci/ci.yml .github/workflows/ci.yml
git add -f .github/workflows/ci.yml   # -f: the path is ignored on purpose, see above
git commit -m "ci: activate pipeline" && git push
```

or paste the file into the GitHub Actions UI (Actions → New workflow → paste). No other change
is needed: the pipeline is self-contained and has no repository secrets.
