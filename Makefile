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
