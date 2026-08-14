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
# STACK_NAME/AWS_REGION default to this project's actual deployed stack -
# override on the command line (e.g. `make sync-site STACK_NAME=other`) if
# ever deploying a second stack (e.g. a scratch/staging one).
STACK_NAME ?= espn-ff-s3-site
AWS_REGION ?= us-west-2

# $BUCKET is looked up from the stack's Outputs (SiteBucketNameOutput)
# rather than hardcoded here, so a bucket rename/redeploy doesn't require
# editing this file - see .claude/TODO-frontend.md's "how does $BUCKET get sourced"
# item.
site-bucket:
	@aws cloudformation describe-stacks \
		--stack-name $(STACK_NAME) \
		--region $(AWS_REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='SiteBucketNameOutput'].OutputValue" \
		--output text

sync-site:
	aws s3 sync site/ "s3://$$($(MAKE) -s site-bucket)/" --region $(AWS_REGION)

sync-config:
	aws s3 cp league_config.json "s3://$$($(MAKE) -s site-bucket)/league_config.json" --region $(AWS_REGION)

# Publishes everything the frontend deploy step owns (site/ + the one
# repo-root config file) in one go. Does NOT run `sam build && sam deploy` -
# that's infra/Lambda code, chain it yourself first if the stack itself also
# changed: `sam build && sam deploy && make publish-site`.
publish-site: sync-site sync-config

.PHONY: site-bucket sync-site sync-config publish-site

# --------------------------------------------------------------------------
# Parallel/branch infra (.claude/DESIGN-incremental-espn-pipeline.md's live-shadow
# validation, rollout step 7) - stands up a FULL SECOND COPY of template.yaml's
# stack (own bucket, own Lambda, own IAM roles, own Secrets Manager secret,
# own EventBridge schedule) under a stack/bucket/function name derived from
# the current git branch, so a feature branch can exercise the real Lambda/
# EventBridge/IAM path end-to-end - not just the scratch-bucket local-Python
# testing this branch has used so far - without ever touching the production
# stack (`espn-ff-s3-site`, the STACK_NAME default above). Every generated
# name below is a real AWS resource identifier (S3 bucket name, Lambda
# function name) so it's sanitized (lowercase, non-alnum -> '-', collapsed,
# trimmed) and truncated - S3 bucket names cap at 63 chars total - from the
# raw branch name rather than used as-is.
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
# trigger is off. AllowV2RootPublish=true (template.yaml) is what makes
# render-branch below actually render real _v2 output on this stack's site -
# safe here specifically because this bucket root isn't production's real
# published data, unlike the default "false" everywhere else. EspnSwid/
# EspnEspnS2 come from the same local ignore/espn_creds.json every other
# local script already reads - no new credential handling introduced for
# this.
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
			AllowV2RootPublish=true \
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
# run every _v2 shadow step in one invocation, or a single legacy step name
# to sanity-check the branch stack's non-shadow path. No default: an empty/
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
# site/index.html/style.css + league_config.json + the two config/ files
# (owner_map.json, weekly_payouts_config.json - needed by the Lambda's own
# config reads, not otherwise covered by publish-site/sync-config, which
# only handle the front-end-facing files) straight to BRANCH_BUCKET_NAME
# (not through publish-site/sync-config, which default to STACK_NAME=
# espn-ff-s3-site - reusing them here without an override would silently
# sync to *production's* bucket instead), then invokes every step needed
# for a fully-rendering page in one call: the three reports with a _v2
# replacement (head_to_head/advanced_history/weekly_summary), published to
# this stack's bucket root too because deploy-branch already set
# AllowV2RootPublish=true, so what renders is the NEW pipeline's real
# output, not a re-run of the unchanged legacy code - plus the reports that
# have no _v2 counterpart at all yet (history, records, owner_habits),
# which only ever write bucket-root regardless of any flag. Re-run any time
# after re-invoking individual _v2 steps if you just want a fresh render of
# what's already been computed, without recomputing everything: `make
# invoke-branch STEPS='[...]'` alone still writes to the branch bucket root
# too, same as it always has - render-branch is a convenience for "give me
# a fully populated site in one shot," not a different upload path.
render-branch:
	aws s3 sync site/ "s3://$(BRANCH_BUCKET_NAME)/" --region $(AWS_REGION)
	aws s3 cp league_config.json "s3://$(BRANCH_BUCKET_NAME)/league_config.json" --region $(AWS_REGION)
	aws s3 cp ignore/owner_map.json "s3://$(BRANCH_BUCKET_NAME)/config/owner_map.json" --region $(AWS_REGION)
	aws s3 cp config/weekly_payouts_config.json "s3://$(BRANCH_BUCKET_NAME)/config/weekly_payouts_config.json" --region $(AWS_REGION)
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
