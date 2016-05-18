import logging
import os
import urllib.error

import tabulate

from esrally import config, driver, exceptions, paths, telemetry, sweeper, reporter, metrics, time
from esrally.mechanic import mechanic
from esrally.utils import process, net, io
# This is one of the few occasions where we really want to use a star import. As new tracks are added we want to "autodiscover" them
from esrally.track import *

logger = logging.getLogger("rally.racecontrol")


class PipelineStep:
    """
    Describes a single step in an execution pipeline, e.g. "build" or "benchmark".
    """
    def __init__(self, name, ctx, command):
        self.name = name
        self.ctx = ctx
        self.command = command

    def run(self, track, challenge=None, car=None):
        if challenge is None and car is None:
            self.command(self.ctx, track)
        else:
            self.command(self.ctx, track, challenge, car)


class ComponentSelector:
    """
    A special step in a pipeline which extracts car and challenge based on the current configuration.
    """
    def __init__(self, ctx, step):
        self.ctx = ctx
        self.step = step

    def run(self, track):
        challenge = self.find_challenge(track)
        car = self.find_car()
        race_paths = paths.Paths(self.ctx.config)
        self.ctx.config.add(config.Scope.challenge, "system", "challenge.root.dir",
                            race_paths.challenge_root(track.name, challenge.name))
        self.ctx.config.add(config.Scope.challenge, "system", "challenge.log.dir",
                            race_paths.challenge_logs(track.name, challenge.name))
        print("Racing on track [%s] and challenge [%s] with car [%s]" % (track.name, challenge.name, car.name))
        self.step.run(track, challenge, car)

    def find_challenge(self, track):
        selected_challenge = self.ctx.config.opts("benchmarks", "challenge")
        for challenge in track.challenges:
            if challenge.name == selected_challenge:
                return challenge

        raise exceptions.ImproperlyConfigured("Unknown challenge [%s] for track [%s]. You can list the available tracks and their "
                                              "challenges with esrally list tracks." % (selected_challenge, track.name))

    def find_car(self):
        selected_car = self.ctx.config.opts("benchmarks", "car")
        for car in track.cars:
            if car.name == selected_car:
                return car
        raise exceptions.ImproperlyConfigured("Unknown car [%s]. You can list the available cars with esrally list cars." % selected_car)


class Pipeline:
    """
    Describes a whole execution pipeline. A pipeline can consist of one or more steps. Each pipeline should contain roughly of the following
    steps:

    * Prepare the benchmark candidate: It can build Elasticsearch from sources, download a ZIP from somewhere etc.
    * Launch the benchmark candidate: This can be done directly, with tools like Ansible or it can assume the candidate is already launched
    * Run the benchmark
    * Report results
    """

    def __init__(self, name, description, steps):
        """
        Creates a new pipeline.

        :param name: A short name of the pipeline. This name will be used to reference it from the command line.
        :param description: A human-readable description what the pipeline does.
        :param steps: The concrete steps to execute. The steps will be executed in the provided order. A pipeline should consist of at
        least one step.
        """
        self.name = name
        self.description = description
        self.steps = steps

    def run(self, track):
        for step in self.steps:
            step.run(track)

#################################################
#
# Internal helper functions for pipeline steps
#
# If a helper function is short enough it can
# also be added just as a lambda.
#
#################################################


def check_can_handle_source_distribution(ctx, track):
    try:
        ctx.config.opts("source", "local.src.dir")
    except config.ConfigError:
        logging.exception("Rally is not configured to build from sources")
        raise exceptions.SystemSetupError("Rally is not setup to build from sources. You can either benchmark a binary distribution or "
                                          "install the required software and reconfigure Rally with esrally --configure.")


def kill(ctx, track):
    # we're very specific which nodes we kill as there is potentially also an Elasticsearch based metrics store running on this machine
    node_prefix = ctx.config.opts("provisioning", "node.name.prefix")
    process.kill_running_es_instances(node_prefix)


def prepare_track(ctx, track):
    ctx.marshal.setup(track)
    race_paths = paths.Paths(ctx.config)
    track_root = race_paths.track_root(track.name)
    ctx.config.add(config.Scope.benchmark, "system", "track.root.dir", track_root)


# benchmark when we provision ourselves
def benchmark_internal(ctx, track, challenge, car):
    ctx.mechanic.start_metrics(track, challenge, car)
    cluster = ctx.mechanic.start_engine(track, challenge, car)
    driver.Driver(ctx.config, cluster, track, challenge).go()
    ctx.mechanic.stop_engine(cluster)
    ctx.mechanic.revise_candidate()
    ctx.mechanic.stop_metrics()


# benchmark assuming Elasticsearch is already running externally
def benchmark_external(ctx, track, challenge, car):
    ctx.mechanic.start_metrics(track, challenge, car)
    cluster = ctx.mechanic.start_engine_external(track, challenge, car)
    driver.Driver(ctx.config, cluster, track, challenge).go()
    ctx.mechanic.stop_metrics()


def download_benchmark_candidate(ctx, track):
    version = ctx.config.opts("source", "distribution.version")
    if version.strip() == "":
        raise exceptions.SystemSetupError("Could not determine version. Please specify the command line the Elasticsearch "
                                          "distribution to download with the command line parameter --distribution-version. "
                                          "E.g. --distribution-version=5.0.0")

    distributions_root = "%s/%s" % (ctx.config.opts("system", "root.dir"), ctx.config.opts("source", "distribution.dir"))
    io.ensure_dir(distributions_root)
    distribution_path = "%s/elasticsearch-%s.tar.gz" % (distributions_root, version)

    download_url = "https://download.elasticsearch.org/elasticsearch/release/org/elasticsearch/distribution/tar/elasticsearch/%s/" \
                   "elasticsearch-%s.tar.gz" % (version, version)
    if not os.path.isfile(distribution_path):
        try:
            print("Downloading Elasticsearch %s ..." % version)
            net.download(download_url, distribution_path)
        except urllib.error.HTTPError:
            logging.exception("Cannot download Elasticsearch distribution for version [%s] from [%s]." % (version, download_url))
            raise exceptions.SystemSetupError("Cannot download Elasticsearch distribution from [%s]. Please check that the specified "
                                              "version [%s] is correct." % (download_url, version))
    else:
        logger.info("Skipping download for version [%s]. Found an existing binary locally at [%s]." % (version, distribution_path))

    ctx.config.add(config.Scope.invocation, "builder", "candidate.bin.path", distribution_path)


# benchmarks with external candidates are really scary and we should warn users.
bogus_results_warning = """
************************************************************************
************** WARNING: A dark dungeon lies ahead of you  **************
************************************************************************

Rally dos not have control over the configuration of the benchmarked
Elasticsearch cluster.

Be aware that results may be misleading due to problems with the setup.
Rally is also not able to gather lots of metrics at all (like CPU usage
of the benchmarked cluster) or may even produce misleading metrics (like
the index size).

************************************************************************
****** Use this pipeline only if you are aware of the tradeoffs.  ******
*************************** Watch your step! ***************************
************************************************************************
"""

pipelines = {
    "from-sources-complete":
        lambda ctx: Pipeline("from-sources-complete", "Builds and provisions Elasticsearch, runs a benchmark and reports results.",
                         [
                             PipelineStep("check-can-handle-sources", ctx, check_can_handle_source_distribution),
                             PipelineStep("kill-es", ctx, kill),
                             PipelineStep("prepare-track", ctx, prepare_track),
                             PipelineStep("build", ctx, lambda ctx, track: ctx.mechanic.prepare_candidate()),
                             PipelineStep("find-candidate", ctx, lambda ctx, track: ctx.mechanic.find_candidate()),
                             ComponentSelector(ctx, PipelineStep("benchmark", ctx, benchmark_internal)),
                             PipelineStep("report", ctx, lambda ctx, track: ctx.reporter.report(track)),
                             PipelineStep("sweep", ctx, lambda ctx, track: ctx.sweeper.run(track))
                         ]
                         ),
    "from-sources-skip-build":
        lambda ctx: Pipeline("from-sources-skip-build", "Provisions Elasticsearch (skips the build), runs a benchmark and reports results.",
                         [
                             PipelineStep("check-can-handle-sources", ctx, check_can_handle_source_distribution),
                             PipelineStep("kill-es", ctx, kill),
                             PipelineStep("prepare-track", ctx, prepare_track),
                             PipelineStep("find-candidate", ctx, lambda ctx, track: ctx.mechanic.find_candidate()),
                             ComponentSelector(ctx, PipelineStep("benchmark", ctx, benchmark_internal)),
                             PipelineStep("report", ctx, lambda ctx, track: ctx.reporter.report(track)),
                             PipelineStep("sweep", ctx, lambda ctx, track: ctx.sweeper.run(track))
                         ]

                         ),
    "from-distribution":
        lambda ctx: Pipeline("from-distribution", "Downloads an Elasticsearch distribution, provisions it, runs a benchmark and reports "
                                                  "results.",
                             [
                                 PipelineStep("kill-es", ctx, kill),
                                 PipelineStep("download-candidate", ctx, download_benchmark_candidate),
                                 PipelineStep("prepare-track", ctx, prepare_track),
                                 ComponentSelector(ctx, PipelineStep("benchmark", ctx, benchmark_internal)),
                                 PipelineStep("report", ctx, lambda ctx, track: ctx.reporter.report(track)),
                                 PipelineStep("sweep", ctx, lambda ctx, track: ctx.sweeper.run(track))
                             ]

                             ),
    "benchmark-only":
        lambda ctx: Pipeline("benchmark-only", "Assumes an already running Elasticsearch instance, runs a benchmark and reports results",
                             [
                                 PipelineStep("warn-bogus", ctx, lambda ctx, track: print(bogus_results_warning)),
                                 PipelineStep("prepare-track", ctx, prepare_track),
                                 ComponentSelector(ctx, PipelineStep("benchmark", ctx, benchmark_external)),
                                 PipelineStep("report", ctx, lambda ctx, track: ctx.reporter.report(track)),
                                 PipelineStep("sweep", ctx, lambda ctx, track: ctx.sweeper.run(track))
                             ]
                             ),

}


class RaceControl:
    def __init__(self, cfg):
        self._config = cfg

    def start(self, command):
        """
        Starts the provided command.

        :param command: A command name.
        :return: True on success, False otherwise
        """
        ctx = RacingContext(self._config)
        try:
            if command == "list":
                self._list(ctx)
            elif command == "race":
                pipeline = self._choose(pipelines, "pipeline", "You can list the available pipelines with esrally list pipelines.")(ctx)
                t = self._choose(track.tracks, "track", "You can list the available tracks with esrally list tracks.")
                metrics.RaceStore(self._config).store_race(t)
                pipeline.run(t)
                return True
            elif command == "compare":
                baseline_ts = self._config.opts("report", "comparison.baseline.timestamp")
                contender_ts = self._config.opts("report", "comparison.contender.timestamp")

                if not baseline_ts or not contender_ts:
                    raise exceptions.ImproperlyConfigured("compare needs baseline and a contender")
                race_store = metrics.RaceStore(self._config)
                reporter.ComparisonReporter(self._config).report(
                    race_store.find_by_timestamp(baseline_ts),
                    race_store.find_by_timestamp(contender_ts))
            else:
                raise exceptions.ImproperlyConfigured("Unknown command [%s]" % command)
        except exceptions.RallyError as e:
            logging.exception("Cannot run benchmark")
            print("\nERROR: Cannot run benchmark\n\nReason: %s" % e)
            return False
        except BaseException as e:
            logging.exception("A fatal error occurred while the running benchmark.")
            raise e

    def _choose(self, source, what, help):
        try:
            name = self._config.opts("system", what)
            return source[name]
        except KeyError:
            raise exceptions.ImproperlyConfigured("Unknown %s [%s]. %s" % (what, name, help))

    def _list(self, ctx):
        what = ctx.config.opts("system", "list.config.option")
        if what == "telemetry":
            print("Available telemetry devices:\n")
            print(tabulate.tabulate(telemetry.Telemetry(ctx.config).list(), ["Command", "Name", "Description"]))
            print("\nKeep in mind that each telemetry device may incur a runtime overhead which can skew results.")
        elif what == "tracks":
            print("Available tracks:\n")
            print(tabulate.tabulate([[t.name, t.short_description, ",".join(map(str, t.challenges))] for t in track.tracks.values()],
                                    headers=["Name", "Description", "Challenges"]))

        elif what == "pipelines":
            print("Available pipelines:\n")
            print(tabulate.tabulate([[pipeline(ctx).name, pipeline(ctx).description] for pipeline in pipelines.values()],
                                    headers=["Name", "Description"]))
        elif what == "races":
            print("Recent races:\n")
            races = []
            for race in metrics.RaceStore(ctx.config).list():
                races.append([time.to_iso8601(race.trial_timestamp), race.track, race.challenge, race.car, race.user_tag])

            print(tabulate.tabulate(races, headers=["Race Timestamp", "Track", "Challenge", "Car", "User Tag"]))
        elif what == "cars":
            #TODO dm: Implement me
            print("Error: Not yet implemented")
        else:
            raise exceptions.ImproperlyConfigured("Cannot list unknown configuration option [%s]" % what)


class RacingContext:
    def __init__(self, cfg):
        self.config = cfg
        self.mechanic = mechanic.Mechanic(cfg)
        self.marshal = track.Marshal(cfg)
        self.reporter = reporter.SummaryReporter(cfg)
        self.sweeper = sweeper.Sweeper(cfg)