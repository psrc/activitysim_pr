# ActivitySim
# See full license in LICENSE.txt.

from __future__ import (absolute_import, division, print_function, )
from future.standard_library import install_aliases
install_aliases()  # noqa: E402

from future.utils import iteritems

from collections import OrderedDict

import logging

import pandas as pd

from activitysim.core import tracing
from activitysim.core import config
from activitysim.core import pipeline
from activitysim.core import simulate
from activitysim.core import inject
from activitysim.core.mem import force_garbage_collect

from activitysim.core.interaction_sample_simulate import interaction_sample_simulate
from activitysim.core.interaction_sample import interaction_sample

from .util import expressions
from .util import logsums as logsum

from activitysim.abm.tables import shadow_pricing

"""
The school/workplace location model predicts the zones in which various people will
work or attend school.
"""

logger = logging.getLogger(__name__)


def spec_for_segment(model_spec, segment_name):

    spec = model_spec[[segment_name]]

    # drop spec rows with zero coefficients since they won't have any effect (0 marginal utility)
    zero_rows = (spec == 0).all(axis=1)
    if zero_rows.any():
        logger.debug("dropping %s all-zero rows from spec" % (zero_rows.sum(),))
        spec = spec.loc[~zero_rows]

    return spec


# we want to iterate over segment_ids in the same order every time
def order_dict_by_keys(segment_ids):
    return OrderedDict([(k, segment_ids[k]) for k in sorted(segment_ids.keys())])


def run_location_sample(
        segment_name,
        persons_merged,
        skim_dict,
        dest_size_terms,
        model_settings,
        chunk_size, trace_hh_id, trace_label):
    """
    build a table of persons * all zones in order to select a sample of alternative locations.

    person_id,  dest_TAZ, rand,            pick_count
    23750,      14,       0.565502716034,  4
    23750,      16,       0.711135838871,  6
    ...
    23751,      12,       0.408038878552,  1
    23751,      14,       0.972732479292,  2
    """
    assert not persons_merged.empty

    model_spec = simulate.read_model_spec(file_name=model_settings['SAMPLE_SPEC'])

    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings['SIMULATE_CHOOSER_COLUMNS']
    choosers = persons_merged[chooser_columns]

    alternatives = dest_size_terms

    sample_size = model_settings["SAMPLE_SIZE"]
    alt_dest_col_name = model_settings["ALT_DEST_COL_NAME"]

    logger.info("Running %s with %d persons" % (trace_label, len(choosers.index)))

    # create wrapper with keys for this lookup - in this case there is a TAZ in the choosers
    # and a TAZ in the alternatives which get merged during interaction
    # the skims will be available under the name "skims" for any @ expressions
    skims = skim_dict.wrap("TAZ", "TAZ_r")

    locals_d = {
        'skims': skims,
        'segment_size': segment_name
    }
    constants = config.get_model_constants(model_settings)
    if constants is not None:
        locals_d.update(constants)

    choices = interaction_sample(
        choosers,
        alternatives,
        sample_size=sample_size,
        alt_col_name=alt_dest_col_name,
        spec=spec_for_segment(model_spec, segment_name),
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=trace_label)

    return choices


def run_location_logsums(
        segment_name,
        persons_merged_df,
        skim_dict, skim_stack,
        location_sample_df,
        model_settings,
        chunk_size, trace_hh_id, trace_label):
    """
    add logsum column to existing location_sample table

    logsum is calculated by running the mode_choice model for each sample (person, dest_taz) pair
    in location_sample, and computing the logsum of all the utilities

    +-----------+--------------+----------------+------------+----------------+
    | PERID     | dest_TAZ     | rand           | pick_count | logsum (added) |
    +===========+==============+================+============+================+
    | 23750     |  14          | 0.565502716034 | 4          |  1.85659498857 |
    +-----------+--------------+----------------+------------+----------------+
    + 23750     | 16           | 0.711135838871 | 6          | 1.92315598631  |
    +-----------+--------------+----------------+------------+----------------+
    + ...       |              |                |            |                |
    +-----------+--------------+----------------+------------+----------------+
    | 23751     | 12           | 0.408038878552 | 1          | 2.40612135416  |
    +-----------+--------------+----------------+------------+----------------+
    | 23751     | 14           | 0.972732479292 | 2          |  1.44009018355 |
    +-----------+--------------+----------------+------------+----------------+
    """

    assert not location_sample_df.empty

    logsum_settings = config.read_model_settings(model_settings['LOGSUM_SETTINGS'])

    # FIXME - MEMORY HACK - only include columns actually used in spec
    persons_merged_df = \
        logsum.filter_chooser_columns(persons_merged_df, logsum_settings, model_settings)

    logger.info("Running %s with %s rows" % (trace_label, len(location_sample_df.index)))

    choosers = pd.merge(location_sample_df,
                        persons_merged_df,
                        left_index=True,
                        right_index=True,
                        how="left")

    tour_purpose = model_settings['LOGSUM_TOUR_PURPOSE']
    if isinstance(tour_purpose, dict):
        tour_purpose = tour_purpose[segment_name]

    logsums = logsum.compute_logsums(
        choosers,
        tour_purpose,
        logsum_settings, model_settings,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id,
        trace_label)

    # "add_column series should have an index matching the table to which it is being added"
    # when the index has duplicates, however, in the special case that the series index exactly
    # matches the table index, then the series value order is preserved
    # logsums now does, since workplace_location_sample was on left side of merge de-dup merge
    location_sample_df['mode_choice_logsum'] = logsums

    return location_sample_df


def run_location_simulate(
        segment_name,
        persons_merged,
        location_sample_df,
        skim_dict,
        dest_size_terms,
        model_settings,
        chunk_size, trace_hh_id, trace_label):
    """
    run location model on location_sample annotated with mode_choice logsum
    to select a dest zone from sample alternatives
    """
    assert not persons_merged.empty

    model_spec = simulate.read_model_spec(file_name=model_settings['SPEC'])

    # FIXME - MEMORY HACK - only include columns actually used in spec
    chooser_columns = model_settings['SIMULATE_CHOOSER_COLUMNS']
    choosers = persons_merged[chooser_columns]

    alt_dest_col_name = model_settings["ALT_DEST_COL_NAME"]

    # alternatives are pre-sampled and annotated with logsums and pick_count
    # but we have to merge additional alt columns into alt sample list

    alternatives = \
        pd.merge(location_sample_df, dest_size_terms,
                 left_on=alt_dest_col_name, right_index=True, how="left")

    logger.info("Running location_simulate with %d persons" % len(choosers))

    # create wrapper with keys for this lookup - in this case there is a TAZ in the choosers
    # and a TAZ in the alternatives which get merged during interaction
    # the skims will be available under the name "skims" for any @ expressions
    skims = skim_dict.wrap("TAZ", alt_dest_col_name)

    locals_d = {
        'skims': skims,
        'segment_size': segment_name
    }
    constants = config.get_model_constants(model_settings)
    if constants is not None:
        locals_d.update(constants)

    choices = interaction_sample_simulate(
        choosers,
        alternatives,
        spec=spec_for_segment(model_spec, segment_name),
        choice_column=alt_dest_col_name,
        skims=skims,
        locals_d=locals_d,
        chunk_size=chunk_size,
        trace_label=trace_label,
        trace_choice_name=model_settings['DEST_CHOICE_COLUMN_NAME'])

    return choices


def run_location_choice(
        persons_merged_df,
        skim_dict, skim_stack,
        dest_size_terms,
        model_settings,
        chunk_size, trace_hh_id, trace_label
        ):

    chooser_segment_column = model_settings['CHOOSER_SEGMENT_COLUMN_NAME']
    segment_ids = model_settings['SEGMENT_IDS']

    # we want to iterate over segment_ids in the same order for replicability
    segment_ids = order_dict_by_keys(model_settings['SEGMENT_IDS'])

    choices_list = []
    for segment_name, segment_id in iteritems(segment_ids):

        choosers = persons_merged_df[persons_merged_df[chooser_segment_column] == segment_id]

        if choosers.shape[0] == 0:
            logger.info("%s skipping segment %s: no choosers", trace_label, segment_name)
            continue

        # - location_sample
        location_sample_df = \
            run_location_sample(
                segment_name,
                choosers,
                skim_dict,
                dest_size_terms,
                model_settings,
                chunk_size,
                trace_hh_id,
                tracing.extend_trace_label(trace_label, 'sample.%s' % segment_name))

        # - location_logsums
        location_sample_df = \
            run_location_logsums(
                segment_name,
                choosers,
                skim_dict, skim_stack,
                location_sample_df,
                model_settings,
                chunk_size,
                trace_hh_id,
                tracing.extend_trace_label(trace_label, 'logsums.%s' % segment_name))

        # - location_simulate
        choices = \
            run_location_simulate(
                segment_name,
                choosers,
                location_sample_df,
                skim_dict,
                dest_size_terms,
                model_settings,
                chunk_size,
                trace_hh_id,
                tracing.extend_trace_label(trace_label, 'simulate.%s' % segment_name))

        choices_list.append(choices)

        # FIXME - want to do this here?
        del location_sample_df
        force_garbage_collect()

    return pd.concat(choices_list) if len(choices_list) > 0 else pd.Series()


def iterate_location_choice(
        model_settings,
        persons_merged, persons,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id, locutor,
        trace_label):

    # column containing segment id
    chooser_segment_column = model_settings['CHOOSER_SEGMENT_COLUMN_NAME']

    # boolean to filter out persons not needing location modeling (e.g. is_worker, is_student)
    chooser_filter_column = model_settings['CHOOSER_FILTER_COLUMN_NAME']

    persons_merged_df = persons_merged.to_frame()

    persons_merged_df = persons_merged_df[persons_merged[chooser_filter_column]]

    spc = shadow_pricing.load_shadow_price_calculator(model_settings)
    max_iterations = spc.max_iterations

    logging.debug("%s max_iterations: %s" % (trace_label, max_iterations))

    choices = None
    for iteration in range(1, max_iterations + 1):

        if spc.use_shadow_pricing and iteration > 1:
            spc.update_shadow_prices()

        choices = run_location_choice(
            persons_merged_df,
            skim_dict, skim_stack,
            spc.shadow_price_adjusted_predicted_size(),
            model_settings,
            chunk_size, trace_hh_id,
            trace_label=tracing.extend_trace_label(trace_label, 'i%s' % iteration))

        choices_df = choices.to_frame('dest_choice')
        choices_df['segment_id'] = \
            persons_merged_df[chooser_segment_column].reindex(choices_df.index)

        spc.set_choices(choices_df)

        if locutor:
            spc.write_trace_files(iteration)

        if spc.use_shadow_pricing and spc.check_fit(iteration):
            logging.info("%s converged after iteration %s" % (trace_label, iteration,))
            break

    # - shadow price table
    if locutor:
        if 'SHADOW_PRICE_TABLE' in model_settings:
            inject.add_table(model_settings['SHADOW_PRICE_TABLE'], spc.shadow_prices)
        if 'MODELED_SIZE_TABLE' in model_settings:
            inject.add_table(model_settings['MODELED_SIZE_TABLE'], spc.modeled_size)

    dest_choice_column_name = model_settings['DEST_CHOICE_COLUMN_NAME']
    tracing.print_summary(dest_choice_column_name, choices, value_counts=True)

    persons_df = persons.to_frame()

    # We only chose school locations for the subset of persons who go to school
    # so we backfill the empty choices with -1 to code as no school location
    NO_DEST_TAZ = -1
    persons_df[dest_choice_column_name] = \
        choices.reindex(persons_df.index).fillna(NO_DEST_TAZ).astype(int)

    # - annotate persons
    expressions.assign_columns(
        df=persons_df,
        model_settings=model_settings.get('annotate_persons'),
        trace_label=tracing.extend_trace_label(trace_label, 'annotate_persons'))

    pipeline.replace_table("persons", persons_df)

    if trace_hh_id:
        tracing.trace_df(persons_df,
                         label=trace_label,
                         warn_if_empty=True)

    return persons_df


@inject.step()
def workplace_location(
        persons_merged, persons,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id, locutor):

    trace_label = 'workplace_location'
    model_settings = config.read_model_settings('workplace_location.yaml')

    iterate_location_choice(
        model_settings,
        persons_merged, persons,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id, locutor, trace_label
    )


@inject.step()
def school_location(
        persons_merged, persons,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id, locutor
        ):

    trace_label = 'school_location'
    model_settings = config.read_model_settings('school_location.yaml')

    iterate_location_choice(
        model_settings,
        persons_merged, persons,
        skim_dict, skim_stack,
        chunk_size, trace_hh_id, locutor, trace_label
    )
