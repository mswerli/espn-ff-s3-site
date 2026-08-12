# The build-DataGeneratorFunction target below is a SAM custom build hook
# (template.yaml's DataGeneratorFunction sets Metadata.BuildMethod:
# makefile) - `sam build` runs `make -f Makefile build-DataGeneratorFunction`
# itself, with $(ARTIFACTS_DIR) pointed at the Lambda build output dir. See
# the Metadata comment in template.yaml for why this exists instead of the
# default Python builder: CodeUri is the whole repo root (lambda_function.py
# lives there per DESIGN.md), which also holds ignore/ - real ESPN
# swid/espn_s2 session cookies - and the default builder's CopySource step
# has no working user-configurable exclude (its .samignore support doesn't
# actually exist in current aws-sam-cli/aws-lambda-builders, despite general
# SAM docs describing it - confirmed by reading that package's source).
#
# So this target is an explicit ALLOW-list: only lambda_function.py,
# who_dat/, and requirements.txt's pip dependencies get copied in - nothing
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
	mkdir -p "$(ARTIFACTS_DIR)/who_dat"
	cp -r who_dat/. "$(ARTIFACTS_DIR)/who_dat/"
	find "$(ARTIFACTS_DIR)/who_dat" -name "__pycache__" -type d -prune -exec rm -rf {} +
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
# Frontend deploy tooling (TODO-frontend.md "Deploy tooling") - these are
# separate from `sam deploy` on purpose (DESIGN.md decision #2): SAM only
# pushes infra + Lambda code, never arbitrary files like index.html/style.css
# or league_config.json, so publishing those is its own explicit step.
#
# STACK_NAME/AWS_REGION default to this project's actual deployed stack -
# override on the command line (e.g. `make sync-site STACK_NAME=other`) if
# ever deploying a second stack (e.g. a scratch/staging one).
STACK_NAME ?= who-dat-infra
AWS_REGION ?= us-west-2

# $BUCKET is looked up from the stack's Outputs (SiteBucketNameOutput)
# rather than hardcoded here, so a bucket rename/redeploy doesn't require
# editing this file - see TODO-frontend.md's "how does $BUCKET get sourced"
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
