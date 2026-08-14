# The build-DataGeneratorFunction target below is a SAM custom build hook
# (template.yaml's DataGeneratorFunction sets Metadata.BuildMethod:
# makefile) - `sam build` runs `make -f Makefile build-DataGeneratorFunction`
# itself, with $(ARTIFACTS_DIR) pointed at the Lambda build output dir. See
# the Metadata comment in template.yaml for why this exists instead of the
# default Python builder: CodeUri is the whole repo root (lambda_function.py
# lives there per .claude/DESIGN.md), which also holds ignore/ - real ESPN
# swid/espn_s2 session cookies - and the default builder's CopySource step
# has no working user-configurable exclude (its .samignore support doesn't
# actually exist in current aws-sam-cli/aws-lambda-builders, despite general
# SAM docs describing it - confirmed by reading that package's source).
#
# So this target is an explicit ALLOW-list: only lambda_function.py,
# league_reports/, and requirements.txt's pip dependencies get copied in - nothing
# else in the repo (ignore/, site/, scripts/, docs, .git, .venv, config/,
# league_config.json, ...) is ever a candidate, regardless of what gets
# added to the repo later. That's the opposite failure mode of a deny-list,
# which only protects against files someone remembered to add to it.
#
# --platform/--implementation/--python-version/--only-binary=:all: force pip
# to fetch prebuilt manylinux wheels for Lambda's actual runtime, not
# whatever the host machine running `sam build` happens to be (e.g. macOS) -
# without this, a dependency with compiled extensions (this project's
# transitive charset-normalizer, for one) would get the wrong platform's
# wheel. Keep manylinux2014_x86_64 in sync with template.yaml's
# Architectures: [x86_64] if that ever changes.
build-DataGeneratorFunction:
	cp lambda_function.py "$(ARTIFACTS_DIR)/"
	mkdir -p "$(ARTIFACTS_DIR)/league_reports"
	cp -r league_reports/. "$(ARTIFACTS_DIR)/league_reports/"
	find "$(ARTIFACTS_DIR)/league_reports" -name "__pycache__" -type d -prune -exec rm -rf {} +
	pip install \
		-r requirements.txt \
		--target "$(ARTIFACTS_DIR)" \
		--no-cache-dir \
		--platform manylinux2014_x86_64 \
		--implementation cp \
		--python-version 3.11 \
		--only-binary=:all: \
		--upgrade

.PHONY: build-DataGeneratorFunction

# --------------------------------------------------------------------------
# Frontend deploy tooling (.claude/TODO-frontend.md "Deploy tooling") - these are
# separate from `sam deploy` on purpose (.claude/DESIGN.md decision #2): SAM only
# pushes infra + Lambda code, never arbitrary files like index.html/style.css
# or league_config.json, so publishing those is its own explicit step.
#
# LEAGUE selects which leagues/<slug>/ directory's config gets pushed, and
# which deployed stack/bucket/function it goes to - looked up from
# leagues/registry.json rather than hardcoded (DESIGN.md decision #15c), so
# adding a league needs no Makefile edit, just a new registry entry. Defaults
# to "who-dat" - this repo's original league - so every existing
# zero-arg command (`make publish-site`, `make sync-payouts-config`, ...)
# keeps working exactly as before. Override per-command: `make publish-site
# LEAGUE=all-for-the-shiva`.
LEAGUE ?= who-dat
AWS_REGION ?= us-west-2

# $(call league-field,<key>) reads one field out of leagues/registry.json for
# the current $(LEAGUE) - a plain `python3 -c` JSON lookup, matching the
# inline-Python style already used elsewhere in this file (e.g.
# deploy-branch's espn_creds.json reads) rather than adding a jq dependency.
league-field = $(shell python3 -c "import json; print(json.load(open('leagues/registry.json'))['$(LEAGUE)']['$(1)'])")

STACK_NAME = $(call league-field,stack_name)
FUNCTION_NAME = $(call league-field,function_name)

site-bucket:
	@python3 -c "import json; print(json.load(open('leagues/registry.json'))['$(LEAGUE)']['site_bucket'])"

sync-site:
	aws s3 sync site/ "s3://$$($(MAKE) -s site-bucket)/" --region $(AWS_REGION)

sync-config:
	aws s3 cp "leagues/$(LEAGUE)/league_config.json" "s3://$$($(MAKE) -s site-bucket)/league_config.json" --region $(AWS_REGION)

# Not front-end-facing (decision #4 - the browser never fetches these, only
# the Lambda reads them, from the config/ prefix per FF_CONFIG_PREFIX) - kept
# separate from sync-config/publish-site for the same reason decision #14
# split out sync-payouts-config originally. owner_map.json holds real names
# (decision #15a), so it's read from ignore/leagues/<slug>/, not the
# committed leagues/<slug>/ - same split as league_reports/config.py's local
# path resolution.
sync-payouts-config:
	aws s3 cp "leagues/$(LEAGUE)/weekly_payouts_config.json" "s3://$$($(MAKE) -s site-bucket)/config/weekly_payouts_config.json" --region $(AWS_REGION)

sync-owner-map:
	aws s3 cp "ignore/leagues/$(LEAGUE)/owner_map.json" "s3://$$($(MAKE) -s site-bucket)/config/owner_map.json" --region $(AWS_REGION)

# Publishes everything the frontend deploy step owns, for $(LEAGUE), in one
# go: site/ + all three config files. Does NOT run `sam build && sam deploy`
# - that's infra/Lambda code; for a brand-new league's first deploy, stand up
# the stack first (`make deploy-league LEAGUE=<slug>`), then this.
publish-site: sync-site sync-config sync-payouts-config sync-owner-map

.PHONY: site-bucket sync-site sync-config sync-payouts-config sync-owner-map publish-site

# --------------------------------------------------------------------------
# Parallel/branch infra - originally built for DESIGN-incremental-espn-pipeline.md's
# live-shadow validation (rollout step 7), still here as general-purpose
# capability now that that particular cutover is done. Stands up a FULL
# SECOND COPY of template.yaml's stack (own bucket, own Lambda, own IAM
# roles, own Secrets Manager secret, own EventBridge schedule) under a
# stack/bucket/function name derived from the current git branch, so a
# feature branch can exercise the real Lambda/EventBridge/IAM path end to
# end - not just local-Python testing - without ever touching the
# production stack (`espn-ff-s3-site`, the STACK_NAME default above). Every
# generated name below is a real AWS resource identifier (S3 bucket name,
# Lambda function name) so it's sanitized (lowercase, non-alnum -> '-',
# collapsed, trimmed) and truncated - S3 bucket names cap at 63 chars total -
# from the raw branch name rather than used as-is.
AWS_ACCOUNT_ID := $(shell aws sts get-caller-identity --query Account --output text)
BRANCH_SLUG := $(shell git rev-parse --abbrev-ref HEAD | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -e 's/-\{2,\}/-/g' -e 's/^-//' -e 's/-$$//' | cut -c1-20)
BRANCH_STACK_NAME := espn-ff-s3-site-$(BRANCH_SLUG)
BRANCH_BUCKET_NAME := espn-ff-site-$(AWS_ACCOUNT_ID)-$(BRANCH_SLUG)
BRANCH_FUNCTION_NAME := espn-ff-data-generator-$(BRANCH_SLUG)

# PandasLayerArn: NOT auto-looked-up. `aws lambda list-layers` only
# enumerates layers owned by *your own* account - the AWS-managed
# AWSSDKPandas-Python311 layer lives in a separate, AWS-owned publisher
# account (336392948345 as of this writing), and that account denies
# cross-account lambda:ListLayerVersions, so there is no CLI call that
# discovers "the latest version" the way [[aws-account-setup]]'s original
# note assumed - confirmed empirically while building this target (the
# list-layers query returns nothing in this account; get-layer-version-by-arn
# against a known version does work, since AWS-managed layers ARE
# cross-account *usable*, just not cross-account *listable*). So: a plain
# override-able default, last confirmed valid via get-layer-version-by-arn
# on the date below. Refresh it by trying successive version numbers
# against get-layer-version-by-arn (or checking
# https://aws-sdk-pandas.readthedocs.io/ 's layer ARN table) when a deploy
# ever fails on this - override with `make deploy-branch PANDAS_LAYER_ARN=...`
# rather than editing this default for a one-off.
PANDAS_LAYER_ARN ?= arn:aws:lambda:us-west-2:336392948345:layer:AWSSDKPandas-Python311:35

# ScheduleState=DISABLED (template.yaml) so a branch stack never runs its own
# unattended weekly ESPN pull on top of production's - the Lambda is still
# fully invokable by name (see invoke-branch below), only the automatic
# trigger is off. EspnSwid/EspnEspnS2 come from the same local
# ignore/espn_creds.json every other local script already reads - no new
# credential handling introduced for this.
deploy-branch:
	@test -f ignore/espn_creds.json || (echo "ignore/espn_creds.json not found - see README.md's Local development step 2" && exit 1)
	@echo "Deploying branch stack: $(BRANCH_STACK_NAME) (bucket $(BRANCH_BUCKET_NAME), function $(BRANCH_FUNCTION_NAME), schedule DISABLED)"
	@SWID=$$(python3 -c "import json; print(json.load(open('ignore/espn_creds.json'))['swid'])"); \
	ESPN_S2=$$(python3 -c "import json; print(json.load(open('ignore/espn_creds.json'))['espn_s2'])"); \
	sam build && \
	sam deploy \
		--stack-name $(BRANCH_STACK_NAME) \
		--region $(AWS_REGION) \
		--resolve-s3 \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-confirm-changeset \
		--parameter-overrides \
			SiteBucketName=$(BRANCH_BUCKET_NAME) \
			FunctionName=$(BRANCH_FUNCTION_NAME) \
			ScheduleState=DISABLED \
			PandasLayerArn=$(PANDAS_LAYER_ARN) \
			EspnSwid=$$SWID \
			EspnEspnS2=$$ESPN_S2
	@echo "Branch stack deployed: $(BRANCH_STACK_NAME)"
	@echo "  make render-branch                                  # push site/ + config, populate real data, render it"
	@echo "  make invoke-branch STEPS='[\"head_to_head_v2\"]'      # invoke just one step"
	@echo "  make destroy-branch                                 # tear it down when done"

# STEPS is the literal JSON list that becomes the Lambda event's "steps" key
# (lambda_function.py's STEPS registry, DESIGN.md decision #10b) - e.g.
# STEPS='["head_to_head_v2","advanced_history_v2","weekly_summary_v2"]' to
# run every _v2 step in one invocation, or a single legacy step name to
# sanity-check the branch stack's legacy path. No default: an empty/
# omitted steps list means "run everything in DEFAULT_STEPS", which for a
# branch stack you almost never want by accident (it's the same expensive
# multi-year legacy pipeline production runs weekly), so this requires it
# explicit rather than silently defaulting.
invoke-branch:
	@test -n "$(STEPS)" || (echo "Usage: make invoke-branch STEPS='[\"head_to_head_v2\"]'" && exit 1)
	aws lambda invoke \
		--function-name $(BRANCH_FUNCTION_NAME) \
		--region $(AWS_REGION) \
		--cli-read-timeout 900 \
		--payload '{"steps": $(STEPS)}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/branch-invoke-response.json
	@cat /tmp/branch-invoke-response.json && echo

# Makes the branch stack's site actually render, end to end: pushes
# site/index.html/style.css + all three of $(LEAGUE)'s config files (DESIGN.md
# decision #15a's leagues/$(LEAGUE)/ + ignore/leagues/$(LEAGUE)/ - defaults to
# who-dat, override with `make render-branch LEAGUE=all-for-the-shiva` to
# test against the other league's config) straight to BRANCH_BUCKET_NAME
# (not through publish-site/sync-config, which resolve their bucket from
# leagues/registry.json - reusing them here would silently sync to a real
# production bucket instead of the throwaway branch one), then invokes every
# step needed for a fully-rendering page in one call: the three reports with
# a _v2 replacement (head_to_head/advanced_history/weekly_summary, cut over
# to production - DESIGN-incremental-espn-pipeline.md decision #13) write
# straight to this stack's bucket root, same as every other step, plus the
# reports that have no _v2 counterpart at all (history, records,
# owner_habits). Re-run any time after re-invoking individual steps if you
# just want a fresh render of what's already been computed, without
# recomputing everything: `make invoke-branch STEPS='[...]'` alone still
# writes to the branch bucket root too, same as it always has -
# render-branch is a convenience for "give me a fully populated site in one
# shot," not a different upload path.
render-branch:
	aws s3 sync site/ "s3://$(BRANCH_BUCKET_NAME)/" --region $(AWS_REGION)
	aws s3 cp "leagues/$(LEAGUE)/league_config.json" "s3://$(BRANCH_BUCKET_NAME)/league_config.json" --region $(AWS_REGION)
	aws s3 cp "ignore/leagues/$(LEAGUE)/owner_map.json" "s3://$(BRANCH_BUCKET_NAME)/config/owner_map.json" --region $(AWS_REGION)
	aws s3 cp "leagues/$(LEAGUE)/weekly_payouts_config.json" "s3://$(BRANCH_BUCKET_NAME)/config/weekly_payouts_config.json" --region $(AWS_REGION)
	$(MAKE) invoke-branch STEPS='["history","records","owner_habits","head_to_head_v2","advanced_history_v2","weekly_summary_v2"]'
	@echo "Branch site: http://$(BRANCH_BUCKET_NAME).s3-website-$(AWS_REGION).amazonaws.com"

# `sam delete` removes the whole stack (bucket, Lambda, roles, secret,
# schedule) in one go, including emptying the bucket first (SAM CLI prompts
# for that; --no-prompts accepts it). See project memory: this account's
# Secrets Manager secret deletions via stack deletion have been observed to
# be immediate rather than the usual 30-day recovery window, so a
# re-deploy-branch under the same branch name right after a destroy-branch
# won't hit a "secret scheduled for deletion" name conflict.
destroy-branch:
	sam delete --stack-name $(BRANCH_STACK_NAME) --region $(AWS_REGION) --no-prompts

.PHONY: deploy-branch invoke-branch render-branch destroy-branch

# --------------------------------------------------------------------------
# Per-league production deploys (DESIGN.md decision #15c) - stands up or
# updates $(LEAGUE)'s real, permanent stack using leagues/registry.json's
# names for it, sharing the exact same template.yaml/Lambda code as every
# other league (no per-site fork). Unlike deploy-branch above, these are
# real production stacks: ScheduleState=ENABLED (the schedule actually runs
# unattended), not DISABLED. EspnSwid/EspnEspnS2 come from the same shared
# ignore/espn_creds.json every league uses (decision #12c/#15a - one ESPN
# login is a member of every league this repo drives). Adding a brand-new
# league is: create leagues/<slug>/ + ignore/leagues/<slug>/owner_map.json,
# add a leagues/registry.json entry, `make deploy-league LEAGUE=<slug>`
# (stands up the stack), `make publish-site LEAGUE=<slug>` (pushes site/ +
# all config, populates real data) - no code change, no template.yaml change.
deploy-league:
	@test -f ignore/espn_creds.json || (echo "ignore/espn_creds.json not found - see README.md's Local development step 2" && exit 1)
	@echo "Deploying league stack: $(STACK_NAME) (bucket $$($(MAKE) -s site-bucket), function $(FUNCTION_NAME), schedule ENABLED)"
	@SWID=$$(python3 -c "import json; print(json.load(open('ignore/espn_creds.json'))['swid'])"); \
	ESPN_S2=$$(python3 -c "import json; print(json.load(open('ignore/espn_creds.json'))['espn_s2'])"); \
	SITE_BUCKET=$$($(MAKE) -s site-bucket); \
	sam build && \
	sam deploy \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION) \
		--resolve-s3 \
		--capabilities CAPABILITY_NAMED_IAM \
		--no-confirm-changeset \
		--parameter-overrides \
			SiteBucketName=$$SITE_BUCKET \
			FunctionName=$(FUNCTION_NAME) \
			ScheduleState=ENABLED \
			PandasLayerArn=$(PANDAS_LAYER_ARN) \
			EspnSwid=$$SWID \
			EspnEspnS2=$$ESPN_S2
	@echo "League stack deployed: $(STACK_NAME)"
	@echo "  make publish-site LEAGUE=$(LEAGUE)   # push site/ + all config, populate real data"

# STEPS works exactly like invoke-branch's (DESIGN.md decision #10b's STEPS
# registry) - explicit and required, no "run everything" default by
# accident.
invoke-league:
	@test -n "$(STEPS)" || (echo "Usage: make invoke-league LEAGUE=<slug> STEPS='[\"head_to_head_v2\"]'" && exit 1)
	aws lambda invoke \
		--function-name $(FUNCTION_NAME) \
		--region $(AWS_REGION) \
		--cli-read-timeout 900 \
		--payload '{"steps": $(STEPS)}' \
		--cli-binary-format raw-in-base64-out \
		/tmp/league-invoke-response.json
	@cat /tmp/league-invoke-response.json && echo

.PHONY: deploy-league invoke-league
