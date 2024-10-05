from aiohttp import ClientSession, BasicAuth
import asyncio
import requests
import bittensor as bt
import torch
import random
import traceback
from typing import List, Optional, Tuple, Type, Union
import datetime as dt
from datetime import timedelta, timezone
from collections import defaultdict
import copy

from common.data import Sport, League, Match, MatchPrediction, ProbabilityChoice, get_probablity_choice_from_string
from common.protocol import GetLeagueCommitments, GetMatchPrediction
import storage.validator_storage as storage
from storage.sqlite_validator_storage import SqliteValidatorStorage

from common.constants import (
    IS_DEV,
    VALIDATOR_TIMEOUT
)

from neurons.validator import Validator
from vali_utils import scoring_utils

# initialize our validator storage class
storage = SqliteValidatorStorage.get_instance()
storage.initialize()


async def sync_match_data(match_data_endpoint) -> bool:
    try:
        async with ClientSession() as session:
            # TODO: add in authentication
            async with session.get(match_data_endpoint) as response:
                response.raise_for_status()
                match_data = await response.json()

        if not match_data or "matches" not in match_data:
            bt.logging.info("No match data returned from API")
            return False

        match_data = match_data["matches"]

        # UPSERT logic
        matches_to_insert = []
        matches_to_update = []
        for item in match_data:
            if "matchId" not in item:
                bt.logging.error(f"Skipping match data missing matchId: {item}")
                continue

            match = Match(
                matchId=item["matchId"],
                matchDate=item["matchDate"],
                sport=item["sport"],
                league=item["matchLeague"],
                homeTeamName=item["homeTeamName"],
                awayTeamName=item["awayTeamName"],
                homeTeamScore=item["homeTeamScore"],
                awayTeamScore=item["awayTeamScore"],
                homeTeamOdds=item["homeTeamOdds"],
                awayTeamOdds=item["awayTeamOdds"],
                drawOdds=item["drawOdds"],
                isComplete=item["isComplete"],
            )
            if storage.check_match(item["matchId"]):
                matches_to_update.append(match)
            else:
                matches_to_insert.append(match)

        if matches_to_insert:
            storage.insert_matches(matches_to_insert)
            bt.logging.info(f"Inserted {len(matches_to_insert)} new matches.")
        if matches_to_update:
            storage.update_matches(matches_to_update)
            bt.logging.info(f"Updated {len(matches_to_update)} existing matches.")

        return True

    except Exception as e:
        bt.logging.error(f"Error getting match data: {e}")
        return False
    
def sync_match_odds_data(match_odds_data_endpoint: str, matchId: str = None) -> Optional[List[Tuple[str, float, float, float, str]]]:
    try:
        # Construct the URL
        if matchId:
            match_odds_data_endpoint = f"{match_odds_data_endpoint}?matchId={matchId}"

        # Make the GET request
        response = requests.get(match_odds_data_endpoint)
        response.raise_for_status()
        odds_data = response.json()

        if not odds_data or "match_odds" not in odds_data:
            bt.logging.debug("No odds data returned from API")
            return None
        
        odds_data = odds_data["match_odds"]
        if len(odds_data) == 0:
            bt.logging.debug("No odds data returned from API")
            return None

        odds_to_insert = []
        for item in odds_data:
            if "matchId" not in item:
                bt.logging.error(f"Skipping odds data missing matchId: {item}")
                continue
            if not storage.check_match_odds(item["matchId"]):
                odds_to_insert.append((
                    item["matchId"],
                    float(item["homeTeamOdds"]),
                    float(item["awayTeamOdds"]),
                    float(item["drawOdds"]),
                    dt.datetime.strptime(item["lastUpdated"], "%Y-%m-%dT%H:%M:%S")
                ))

        if odds_to_insert:
            storage.insert_match_odds(odds_to_insert)
            bt.logging.info(f"Inserted {len(odds_to_insert)} odds for matches.")

        return odds_to_insert

    except requests.RequestException as e:
        bt.logging.error(f"Error getting odds data: {e}")
        return None

    except Exception as e:
        bt.logging.error(f"Unexpected error getting odds data: {e}")
        return None

async def process_app_prediction_requests(
    vali: Validator,
    app_prediction_requests_endpoint: str,
    app_prediction_responses_endpoint: str,
) -> bool:
    keypair = vali.dendrite.keypair
    hotkey = keypair.ss58_address
    signature = f"0x{keypair.sign(hotkey).hex()}"
    try:
        async with ClientSession() as session:
            async with session.get(
                app_prediction_requests_endpoint, auth=BasicAuth(hotkey, signature)
            ) as response:
                response.raise_for_status()
                prediction_requests = await response.json()

        if not prediction_requests or "requests" not in prediction_requests:
            bt.logging.info("No app prediction requests returned from API")
            return False

        prediction_requests = prediction_requests["requests"]

        prediction_responses = []
        bt.logging.info(
            f"Sending {len(prediction_requests)} app requests to miners for predictions."
        )
        for pr in prediction_requests:
            match_prediction = MatchPrediction(
                matchId=pr["matchId"],
                matchDate=pr["matchDate"],
                sport=pr["sport"],
                league=pr["league"],
                homeTeamName=pr["homeTeamName"],
                awayTeamName=pr["awayTeamName"],
            )
            miner_hotkey = pr["miner_hotkey"]
            if IS_DEV:
                miner_uids = [9999]
            else:
                if miner_hotkey in vali.metagraph.hotkeys:
                    miner_uids = [
                        vali.metagraph.hotkeys.index(miner_hotkey) 
                    ]

            if len(miner_uids) > 0:
                bt.logging.info(
                    f"-- Sending match to miners {miner_uids} for predictions."
                )
                input_synapse = GetMatchPrediction(match_prediction=match_prediction)
                # Send prediction requests to miners and store their responses. TODO: do we need to mark the stored prediction as being an app request prediction? not sure it matters
                finished_responses, working_miner_uids = await send_predictions_to_miners(
                    vali, input_synapse, miner_uids
                )
                # Add the responses to the list of responses
                for response in finished_responses:
                    # Extract match_prediction and add app_request_id
                    match_prediction = response.match_prediction
                    match_prediction_dict = match_prediction.__dict__
                    match_prediction_dict["app_request_id"] = pr["app_request_id"]
                    match_prediction_dict["matchDate"] = str(match_prediction_dict["matchDate"]), # convert matchDate to string for serialization
                    prediction_responses.append(match_prediction_dict)

                # Loop through miners not responding and add them to the response list with a flag
                for uid in miner_uids:
                    if uid not in working_miner_uids:
                        match_prediction_not_working = pr
                        match_prediction_not_working["minerHasIssue"] = True
                        match_prediction_not_working["minerIssueMessage"] = "Miner did not respond with a prediction."
                        prediction_responses.append(match_prediction_not_working)

            else:
                bt.logging.info(
                    f"-- No miner uid found for {miner_hotkey}, skipping."
                )

        if len(prediction_responses) > 0:
            max_retries = 3
            # Post the prediction responses back to API
            for attempt in range(max_retries):
                try:
                    # Attempt to post the prediction responses
                    post_result = await post_app_prediction_responses(
                        vali, app_prediction_responses_endpoint, prediction_responses
                    )
                    return post_result
                except Exception as e:
                    bt.logging.error(f"Attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        # Wait before retrying
                        await asyncio.sleep(2)
                    else:
                        # Raise the exception if the maximum number of retries is reached
                        bt.logging.error(
                            f"Failed to post app prediction responses after {max_retries} attempts."
                        )
                        # Return False to indicate that the post failed
                        return False

        return True

    except Exception as e:
        bt.logging.error(f"Error processing app prediction requests: {e}")
        return False


async def post_app_prediction_responses(
    vali, prediction_responses_endpoint, prediction_responses
):
    keypair = vali.dendrite.keypair
    hotkey = keypair.ss58_address
    signature = f"0x{keypair.sign(hotkey).hex()}"
    try:
        # Post the app prediction request responses back to the api
        """
        prediction_responses = [
            {
                "app_request_id": "frontend-12345",
                "match_prediction":{
                    "matchId": "TeamATeamB202408011530",
                    "matchDate": "2024-08-01 15:30:00",
                    "sport": "Baseball",
                    "league": "MLB",
                    "homeTeamName": "Team A",
                    "awayTeamName": "Team B",
                    "homeTeamScore": 2,
                    "awayTeamScore": 3,
                    "isComplete": 1,
                    "miner_hotkey": "hotkey123"
                }
            }
        ]
        """
        async with ClientSession() as session:
            async with session.post(
                prediction_responses_endpoint,
                auth=BasicAuth(hotkey, signature),
                json=prediction_responses,
            ) as response:
                response.raise_for_status()
                bt.logging.info("Successfully posted app prediction responses to API.")
                return True

    except Exception as e:
        bt.logging.error(f"Error posting app prediction responses to API: {e}")
        return False


async def send_league_commitments_to_miners(
    vali: Validator, input_synapse: GetLeagueCommitments, miner_uids: List[int]
):
    try:
        random.shuffle(miner_uids)
        axons = [vali.metagraph.axons[uid] for uid in miner_uids]
        
        responses = await vali.dendrite(
            # Send the query to selected miner axons in the network.
            axons=axons,
            synapse=input_synapse,
            deserialize=True,
            timeout=VALIDATOR_TIMEOUT,
        )

        working_miner_uids = []
        finished_responses = []
        uid_league_updates = {}
        for response in responses:
            
            if (
                response is None
                or response.leagues is None
                or response.axon is None
                or response.axon.hotkey is None
            ):
                bt.logging.info(
                    f"{response.axon.hotkey}: Miner failed to respond to league commitments."
                )
                continue
            else:
                uid = [
                    uid
                    for uid, axon in zip(miner_uids, axons)
                    if axon.hotkey == response.axon.hotkey
                ][0]
                working_miner_uids.append(uid)
                finished_responses.append(response)
                # check if the leagues are valid. throw out any invalid leagues
                valid_leagues = []
                for league in response.leagues:
                    if league in League:
                        valid_leagues.append(league)
                    else:
                        bt.logging.info(
                            f"{response.axon.hotkey}: League {league} is not valid. Throwing out."
                        )
                uid_league_updates[uid] = valid_leagues

        if len(working_miner_uids) == 0:
            bt.logging.info("No miner responses available.")
            return (finished_responses, working_miner_uids)

        bt.logging.info(f"Received responses: {responses}")
        bt.logging.info(f"Storing miner league commitments to validator storage.")
        # Bulk update of uids_to_leagues
        with vali.uids_to_leagues_lock:
            for uid, leagues in uid_league_updates.items():
                vali.uids_to_leagues[uid] = leagues

        return

    except Exception:
        bt.logging.error(
            f"Failed to send predictions to miners and store in validator database.",
            traceback.format_exc(),
        )
        return None


def get_match_prediction_requests(vali: Validator) -> List[MatchPrediction]:
    # Get all eligible matches that can be predicted
    matches = storage.get_matches_to_predict()
    if not matches:
        return []
    
    current_time = dt.datetime.now(dt.timezone.utc)
    # Get all the match prediction request data
    match_prediction_requests = storage.get_match_prediction_requests()

    match_predictions = []
    for match in matches:
        # Skip any matches from non-active leagues
        if match.league not in vali.ACTIVE_LEAGUES:
            continue

        # If the match is not in the match_prediction_requests, add it
        if match.matchId not in match_prediction_requests:
            match_prediction_requests[match.matchId] = {
                '24_hour': False,
                '12_hour': False,
                '4_hour': False,
                '10_min': False
            }

        # Calculate the time until the match
        match_date_aware = match.matchDate.replace(tzinfo=timezone.utc)
        time_until_match = match_date_aware - current_time

        """
        print(f"Match: {match.matchId}, Time until match: {time_until_match}")
        print(f"24_hour: {match_prediction_requests[match.matchId]['24_hour']}")
        print(f"12_hour: {match_prediction_requests[match.matchId]['12_hour']}")
        print(f"4_hour: {match_prediction_requests[match.matchId]['4_hour']}")
        print(f"10_min: {match_prediction_requests[match.matchId]['10_min']}")
        """

        # Define prediction windows
        prediction_windows = [
            ('24_hour', timedelta(hours=24), timedelta(hours=23), 'prediction_24_hour'),
            ('12_hour', timedelta(hours=12), timedelta(hours=11), 'prediction_12_hour'),
            ('4_hour', timedelta(hours=4), timedelta(hours=3), 'prediction_4_hour'),
            ('10_min', timedelta(minutes=15), timedelta(minutes=5), 'prediction_10_min')
        ]

        # Create match predictions for each prediction window, if the match has not been predicted in that window
        for window, upper_bound, lower_bound, update_key in prediction_windows:
            if (not match_prediction_requests[match.matchId][window] and
                upper_bound >= time_until_match > lower_bound):
                bt.logging.debug(f"Match found in prediction window {window}: {match.awayTeamName} at {match.homeTeamName} on {match.matchDate}")
                match_predictions.append(
                    MatchPrediction(
                        matchId=match.matchId,
                        matchDate=str(match.matchDate),
                        sport=match.sport,
                        league=match.league,
                        homeTeamName=match.homeTeamName,
                        awayTeamName=match.awayTeamName
                    )
                )
                storage.update_match_prediction_request(match.matchId, update_key)
                break  # Only one prediction per match per cycle
    
    return match_predictions


async def send_predictions_to_miners(
    vali: Validator, input_synapse: GetMatchPrediction, miner_uids: List[int]
) -> Tuple[List[MatchPrediction], List[int]]:
    try:
        if IS_DEV:
            # For now, just return a list of random MatchPrediction responses
            responses = [
                GetMatchPrediction(
                    match_prediction=MatchPrediction(
                        matchId=input_synapse.match_prediction.matchId,
                        matchDate=input_synapse.match_prediction.matchDate,
                        sport=input_synapse.match_prediction.sport,
                        league=input_synapse.match_prediction.league,
                        homeTeamName=input_synapse.match_prediction.homeTeamName,
                        awayTeamName=input_synapse.match_prediction.awayTeamName,
                        homeTeamScore=random.randint(0, 10),
                        awayTeamScore=random.randint(0, 10),
                    )
                )
                for uid in miner_uids
            ]
        else:

            random.shuffle(miner_uids)
            axons = [vali.metagraph.axons[uid] for uid in miner_uids]

            # convert matchDate to string for serialization
            input_synapse.match_prediction.matchDate = str(
                input_synapse.match_prediction.matchDate
            )
            responses = await vali.dendrite(
                # Send the query to selected miner axons in the network.
                axons=axons,
                synapse=input_synapse,
                deserialize=True,
                timeout=VALIDATOR_TIMEOUT,
            )

        working_miner_uids = []
        finished_responses = []
        for response in responses:
            is_prediction_valid, error_msg = is_match_prediction_valid(
                response.match_prediction,
                input_synapse,
            )
            if IS_DEV:
                uid = miner_uids.pop(random.randrange(len(miner_uids)))
                working_miner_uids.append(uid)
                finished_responses.append(response)
            else:
                if (
                    response is None
                    or response.match_prediction is None
                    or response.match_prediction.probabilityChoice is None
                    or response.match_prediction.probability is None
                    or response.axon is None
                    or response.axon.hotkey is None
                ):
                    bt.logging.info(
                        f"{response.axon.hotkey}: Miner failed to respond with a valid prediction."
                    )
                    continue
                elif not is_prediction_valid:
                    bt.logging.info(
                        f"{response.axon.hotkey}: Miner prediction failed validation: {error_msg}"
                    )
                    continue
                else:
                    uid = [
                        uid
                        for uid, axon in zip(miner_uids, axons)
                        if axon.hotkey == response.axon.hotkey
                    ][0]
                    working_miner_uids.append(uid)
                    response.match_prediction.minerId = uid
                    response.match_prediction.hotkey = response.axon.hotkey
                    response.match_prediction.predictionDate = dt.datetime.now(dt.timezone.utc)
                    finished_responses.append(response)

        if len(working_miner_uids) == 0:
            bt.logging.info("No miner responses available.")
            return (finished_responses, working_miner_uids)

        bt.logging.info(f"Received responses: {redact_scores(responses)}")
        # store miner predictions in validator database to be scored when applicable
        bt.logging.info(f"Storing predictions in validator database.")
        storage.insert_match_predictions(finished_responses)

        return (finished_responses, working_miner_uids)

    except Exception:
        bt.logging.error(
            f"Failed to send predictions to miners and store in validator database.",
            traceback.format_exc(),
        )
        return None


def clean_up_unscored_deregistered_match_predictions(active_miner_hotkeys: List[str], active_miner_uids: List[int]):
    """Deletes unscored predictions returned from miners that are no longer registered."""
    try:
        storage.delete_unscored_deregistered_match_predictions(active_miner_hotkeys, active_miner_uids)
    except Exception as e:
        bt.logging.error(f"Error cleaning up unscored deregistered predictions: {e}")


def archive_deregistered_match_predictions(active_miner_hotkeys: List[str], active_miner_uids: List[int]):
    """Archives predictions from miners that are no longer registered."""
    try:
        storage.archive_match_predictions(active_miner_hotkeys, active_miner_uids)
    except Exception as e:
        bt.logging.error(f"Error archiving unscored deregistered predictions: {e}")


def find_and_score_edge_match_predictions(batchsize: int) -> Tuple[List[float], List[int], List[int], List[str], List[str]]:
    """Query the validator's local storage for a list of qualifying MatchPredictions that can be scored.

    Then run CLV Edge calculations and return results
    """

    # Query for scorable match predictions with actual match data
    predictions_with_match_data = storage.get_match_predictions_to_score(batchsize)

    edge_scores = []
    correct_winner_results = []
    miner_uids = []
    predictions = []
    sports = []
    leagues = []
    for pwmd in predictions_with_match_data:
        prediction = pwmd.prediction
        uid = prediction.minerId

        # Calculate the CLV Edge for the prediction
        edge, correct_winner_score = scoring_utils.calculate_edge(
            prediction_team=prediction.get_predicted_team(),
            prediction_prob=prediction.probability,
            actual_team=pwmd.get_actual_winner(),
            consensus_closing_odds=pwmd.get_actual_winner_odds(),
        )
        prediction.closingEdge = edge
        
        edge_scores.append(edge)
        correct_winner_results.append(correct_winner_score)
        miner_uids.append(uid)
        predictions.append(prediction)
        sports.append(prediction.sport)
        leagues.append(prediction.league)

    # mark predictions as scored in the local db
    if len(predictions) > 0:
        storage.update_match_predictions(predictions)

    return [
        edge_scores,
        correct_winner_results,
        miner_uids,
        sports,
        leagues,
    ]


def is_match_prediction_valid(
    prediction: MatchPrediction, input_synapse: GetMatchPrediction
) -> Tuple[bool, str]:
    """Performs basic validation on a MatchPrediction.

    Returns a tuple of (is_valid, reason) where is_valid is True if the entities are valid,
    and reason is a string describing why they are not valid.
    """

    # Check if probabilityChoice is None
    if prediction.probabilityChoice is None:
        return (
            False,
            "Probability choice is None",
        )
    # Check the validity of the probability predictions
    if isinstance(prediction.probabilityChoice, str):
        if get_probablity_choice_from_string(prediction.probabilityChoice) is None:
            return (
                False,
                f"Probability choice {prediction.probabilityChoice} is not a valid choice",
            )
    elif isinstance(prediction.probabilityChoice, ProbabilityChoice):
        probability_choice = prediction.probabilityChoice
    else:
        return (
            False,
            f"Probability choice {prediction.probabilityChoice} is not of type ProbabilityChoice or str",
        )
    
    if not isinstance(prediction.probability, float):
        return (
            False,
            f"Probability {prediction.probability} is not a float",
        )
    if prediction.probability < 0 or prediction.probability > 1:
        return (
            False,
            f"Probability {prediction.probability} is not between 0 and 1",
        )
    """ Turning off homeTeamScore and awayTeamScore validation as it will no longer be needed.
    # Check the validity of the scores
    if not isinstance(prediction.homeTeamScore, int):
        return (
            False,
            f"Home team score {prediction.homeTeamScore} is not an integer",
        )
    if prediction.homeTeamScore < 0:
        return (
            False,
            f"Home team score {prediction.homeTeamScore} is a negative integer",
        )

    if not isinstance(prediction.awayTeamScore, int):
        return (
            False,
            f"Away team score {prediction.awayTeamScore} is not an integer",
        )
    if prediction.awayTeamScore < 0:
        return (
            False,
            f"Away team score {prediction.awayTeamScore} is a negative integer",
        )
    """

    # Check that the current time is before the match date
    current_time = dt.datetime.now(dt.timezone.utc)
    # Ensure prediction.matchDate is offset-aware
    if prediction.matchDate.tzinfo is None:
        prediction_match_date = prediction.matchDate.replace(tzinfo=dt.timezone.utc)
    else:
        prediction_match_date = prediction.matchDate
    if current_time >= prediction_match_date:
        return (
            False,
            f"Current time {current_time} is not before start of match date {prediction_match_date}",
        )

    # Check that the prediction response matches the prediction request
    if (
        prediction.matchId != input_synapse.match_prediction.matchId
        or str(prediction.matchDate) != str(input_synapse.match_prediction.matchDate)
        or prediction.sport != input_synapse.match_prediction.sport
        or prediction.league != input_synapse.match_prediction.league
        or prediction.homeTeamName != input_synapse.match_prediction.homeTeamName
        or prediction.awayTeamName != input_synapse.match_prediction.awayTeamName
        or prediction.closingEdge != input_synapse.match_prediction.closingEdge # closingEdge is not part of the request and should be ignored/None
    ):
        return (
            False,
            f"Prediction response does not match prediction request",
        )

    return (True, "")


async def post_prediction_edge_results(
    vali,
    prediction_edge_results_endpoint,
    edge_scores,
    correct_winner_results,
    prediction_uids,
    prediction_hotkeys,
    prediction_sports,
    prediction_leagues,
):
    keypair = vali.dendrite.keypair
    hotkey = keypair.ss58_address
    signature = f"0x{keypair.sign(hotkey).hex()}"
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # Post the scoring results back to the api
            scoring_results = {
                "scores": edge_scores,
                "correct_winner_results": correct_winner_results,
                "uids": prediction_uids,
                "hotkeys": prediction_hotkeys,
                "sports": prediction_sports,
                "leagues": prediction_leagues,
            }
            async with ClientSession() as session:
                async with session.post(
                    prediction_edge_results_endpoint,
                    auth=BasicAuth(hotkey, signature),
                    json=scoring_results,
                ) as response:
                    response.raise_for_status()
                    bt.logging.info("Successfully posted prediction edge results to API.")
                    return response

        except Exception as e:
            bt.logging.error(
                f"Error posting prediction edge results to API, attempt {attempt + 1}: {e}"
            )
            if attempt < max_retries - 1:
                # Wait before retrying
                await asyncio.sleep(2)
            else:
                bt.logging.error(
                    f"Max retries attempted posting prediction edge results to API. Contact a Sportstensor admin."
                )


async def post_prediction_results(
    vali,
    prediction_results_endpoint,
    prediction_scores,
    correct_winner_results,
    prediction_rewards_uids,
    prediction_results_hotkeys,
    prediction_sports,
    prediction_leagues,
):
    keypair = vali.dendrite.keypair
    hotkey = keypair.ss58_address
    signature = f"0x{keypair.sign(hotkey).hex()}"
    max_retries = 3

    for attempt in range(max_retries):
        try:
            # Post the scoring results back to the api
            scoring_results = {
                "scores": prediction_scores,
                "correct_winner_results": correct_winner_results,
                "uids": prediction_rewards_uids,
                "hotkeys": prediction_results_hotkeys,
                "sports": prediction_sports,
                "leagues": prediction_leagues,
            }
            async with ClientSession() as session:
                async with session.post(
                    prediction_results_endpoint,
                    auth=BasicAuth(hotkey, signature),
                    json=scoring_results,
                ) as response:
                    response.raise_for_status()
                    bt.logging.info("Successfully posted prediction results to API.")
                    return response

        except Exception as e:
            bt.logging.error(
                f"Error posting prediction results to API, attempt {attempt + 1}: {e}"
            )
            if attempt < max_retries - 1:
                # Wait before retrying
                await asyncio.sleep(2)
            else:
                bt.logging.error(
                    f"Max retries attempted posting prediction results to API. Contact a Sportstensor admin."
                )


def redact_scores(responses):
    redacted_responses = []
    for response in responses:
        # Create a copy of the response to avoid modifying the original
        redacted_response = copy.deepcopy(response)

        # Redact the homeTeamScore, awayTeamScore, probabilityChoice, and probability fields
        if (
            hasattr(redacted_response.match_prediction, "homeTeamScore")
            and redacted_response.match_prediction.homeTeamScore is not None
        ):
            redacted_response.match_prediction.homeTeamScore = "REDACTED"
        if (
            hasattr(redacted_response.match_prediction, "awayTeamScore")
            and redacted_response.match_prediction.awayTeamScore is not None
        ):
            redacted_response.match_prediction.awayTeamScore = "REDACTED"
        if (
            hasattr(redacted_response.match_prediction, "probabilityChoice")
            and redacted_response.match_prediction.probabilityChoice is not None
        ):
            redacted_response.match_prediction.probabilityChoice = "REDACTED"
        if (
            hasattr(redacted_response.match_prediction, "probability")
            and redacted_response.match_prediction.probability is not None
        ):
            redacted_response.match_prediction.probability = "REDACTED"

        redacted_responses.append(redacted_response)
    return redacted_responses


def check_uid_availability(
    metagraph: "bt.metagraph.Metagraph", uid: int, vpermit_tao_limit: int
) -> bool:
    """Check if uid is available. The UID should be available if it is serving and has less than vpermit_tao_limit stake
    Args:
        metagraph (:obj: bt.metagraph.Metagraph): Metagraph object
        uid (int): uid to be checked
        vpermit_tao_limit (int): Validator permit tao limit
    Returns:
        bool: True if uid is available, False otherwise
    """
    # Filter non serving axons.
    if not metagraph.axons[uid].is_serving:
        return False
    # Filter validator permit > 1024 stake.
    if metagraph.validator_permit[uid]:
        if metagraph.S[uid] > vpermit_tao_limit:
            return False
    # Available otherwise.
    return True


def get_random_uids(self, k: int, exclude: List[int] = None) -> List[int]:
    """Returns k available random uids from the metagraph.
    Args:
        k (int): Number of uids to return.
        exclude (List[int]): List of uids to exclude from the random sampling.
    Returns:
        uids (List[int]): Randomly sampled available uids.
    Notes:
        If `k` is larger than the number of available `uids`, set `k` to the number of available `uids`.
    """
    candidate_uids = []
    avail_uids = []

    for uid in range(self.metagraph.n.item()):
        uid_is_available = check_uid_availability(
            self.metagraph, uid, self.config.neuron.vpermit_tao_limit
        )
        uid_is_not_excluded = exclude is None or uid not in exclude

        if uid_is_available:
            avail_uids.append(uid)
            if uid_is_not_excluded:
                candidate_uids.append(uid)

    # Check if candidate_uids contain enough for querying, if not grab all available uids
    available_uids = candidate_uids

    # Only grab random set of uids if k is greater than 0. allows to send all by passing in -1
    if k > 0:
        if len(candidate_uids) < k:
            new_avail_uids = [uid for uid in avail_uids if uid not in candidate_uids]
            available_uids += random.sample(
                new_avail_uids,
                min(len(new_avail_uids), k - len(candidate_uids)),
            )
        uids = random.sample(available_uids, min(k, len(available_uids)))
    else:
        uids = available_uids

    return uids
