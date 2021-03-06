import datetime
import time
import pytz
import numpy as np

from helpers.match_manipulator import MatchManipulator


class MatchTimePredictionHelper(object):

    EPOCH = datetime.datetime.fromtimestamp(0)
    MAX_IN_PAST = datetime.timedelta(minutes=-3)  # One match length, ish

    @classmethod
    def as_local(cls, time, timezone):
        return pytz.utc.localize(time).astimezone(timezone)

    @classmethod
    def as_utc(cls, time):
        if time.utcoffset():
            return (time - time.utcoffset()).replace(tzinfo=None)
        return time

    @classmethod
    def timestamp(cls, d):
        return time.mktime(d.timetuple())

    @classmethod
    def compute_average_cycle_time(cls, played_matches, next_unplayed, timezone):
        """
        Compute the average cycle time of the given matches, but only for the current day
        :param played_matches: The matches for this event that have been played
        :param next_unplayed: The next match to be played
        :param timezone: The timezone object, for computing local times
        :return: The average cycle time, in seconds, or None if not enough info
        """
        cycles = []

        # Sort matches by when they were actually played
        # This should account for out of order replays messing with the computations
        played_matches.sort(key=lambda x: x.actual_time)

        # Next match start time (in local time)
        next_match_start = cls.as_local(next_unplayed.time, timezone)

        # Find the first played match of the same day as the next match to be played
        start_of_day = None
        for i in range(0, len(played_matches)):
            scheduled_time = cls.as_local(played_matches[i].time, timezone)
            if scheduled_time.day == next_match_start.day:
                start_of_day = i
                break

        if start_of_day is None:
            return None

        # Compute cycle times for matches on this day
        for i in range(start_of_day + 1, len(played_matches)):
            cycle = cls.timestamp(played_matches[i].actual_time) - cls.timestamp(played_matches[i - 1].actual_time)

            # Discard (with 0 weight) outlier cycles that take too long (>150% of the schedule)
            # We want to bias our average to be low, so we don't "overshoot" our predictions
            # So we simply discard outliers instead of letting them skew the average
            # Additionally, discard matches with breaks (like lunch) in between. We find those
            # when we see a scheduled time between matches larger than 15 minutes
            scheduled_cycle = cls.timestamp(played_matches[i].time) - cls.timestamp(played_matches[i - 1].time)
            if scheduled_cycle < 15 * 60 and cycle <= scheduled_cycle * 1.5:
                # Bias the times towards the schedule
                cycle = (0.7 * cycle) + (0.3 * scheduled_cycle)
                cycles.append(cycle)

        return np.percentile(cycles, 35) if cycles else None

    @classmethod
    def predict_future_matches(cls, played_matches, unplayed_matches, timezone, is_live):
        """
        Add match time predictions for future matches
        """
        last_match = played_matches[-1] if played_matches else None
        next_match = unplayed_matches[0] if unplayed_matches else None

        if not next_match:
            # Nothing to predict
            return

        last_match_day = cls.as_local(last_match.time, timezone).day if last_match else None
        average_cycle_time = cls.compute_average_cycle_time(played_matches, next_match, timezone)
        last = last_match

        # Only predict up to 20 matches in the future on the same day
        for i in range(0, min(20, len(unplayed_matches))):
            match = unplayed_matches[i]
            scheduled_time = cls.as_local(match.time, timezone)
            if scheduled_time.day != last_match_day and last_match_day is not None:
                # Stop, once we exhaust all unplayed matches on this day
                break

            # For the first iteration, base the predictions off the newest known actual start time
            # Otherwise, use the predicted start time of the previously processed match
            last_predicted = None
            if last_match:
                last_predicted = cls.as_local(last_match.actual_time if i == 0 else last.predicted_time, timezone)
            if last_predicted and average_cycle_time:
                predicted = last_predicted + datetime.timedelta(seconds=average_cycle_time)
            else:
                predicted = match.time

            # Never predict a match to happen more than 2 minutes ahead of schedule or in the past
            # However, if the event is not live (we're running the job manually for a single event),
            # then allow predicted times to be in the past.
            now = datetime.datetime.now(timezone) + cls.MAX_IN_PAST if is_live else cls.as_local(cls.EPOCH, timezone)
            earliest_possible = cls.as_local(match.time + datetime.timedelta(minutes=-2), timezone)
            match.predicted_time = max(cls.as_utc(predicted), cls.as_utc(earliest_possible), cls.as_utc(now))
            last = match

        MatchManipulator.createOrUpdate(unplayed_matches)
