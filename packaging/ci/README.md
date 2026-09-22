# GitHub Actions workflows - copy into .github/workflows/

These two files belong in `.github/workflows/` at the root of the repository
(`build.yml` builds the Windows installer and the Linux/SteamOS installer on
every push; `unit_tests.yml` runs the tests).

They were placed here because workflow files cannot be written by remote
tools. Copy them into `.github\workflows\` (overwrite the old
`unit_tests.yml`) and commit, and GitHub will start building.
