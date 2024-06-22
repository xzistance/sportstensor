from aiohttp import ClientSession, BasicAuth
import bittensor as bt
import torch
import random
import traceback
from typing import List, Optional, Tuple, Type, Union
import datetime as dt
from collections import defaultdict

from common.data import Sport, Match, Prediction, MatchPrediction
from common.protocol import GetMatchPrediction
import storage.validator_storage as storage
from storage.sqlite_validator_storage import SqliteValidatorStorage

from common.constants import (
    IS_DEV,
    CORRECT_MATCH_WINNER_SCORE,
    MAX_SCORE_DIFFERENCE,
    MAX_SCORE_DIFFERENCE_SOCCER,
    MAX_SCORE_DIFFERENCE_FOOTBALL,
    MAX_SCORE_DIFFERENCE_BASEBALL,
    MAX_SCORE_DIFFERENCE_BASKETBALL,
    MAX_SCORE_DIFFERENCE_CRICKET
)

from neurons.validator import Validator

# initialize our validator storage class
storage = SqliteValidatorStorage()

async def sync_match_data(match_data_endpoint) -> bool:
    try:
        async with ClientSession() as session:
            # TODO: add in authentication?
            async with session.get(match_data_endpoint) as response:
                response.raise_for_status()
                match_data = await response.json()
        
        if not match_data or 'matches' not in match_data:
            bt.logging.info("No match data returned from API")
            return False
        
        match_data = match_data['matches']
        
        # UPSERT logic
        matches_to_insert = []
        matches_to_update = []
        for item in match_data:
            if 'matchId' not in item:
                bt.logging.error(f"Skipping match data missing matchId: {item}")
                continue

            match = Match(
                matchId=item['matchId'],
                matchDate=item['matchDate'],
                sport=item['sport'],
                homeTeamName=item['homeTeamName'],
                awayTeamName=item['awayTeamName'],
                homeTeamScore=item['homeTeamScore'],
                awayTeamScore=item['awayTeamScore'],
                isComplete=item['isComplete']
            )
            if storage.check_match(item['matchId']):
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
    
async def process_app_prediction_requests(vali: Validator, app_prediction_requests_endpoint: str) -> bool:
    try:
        async with ClientSession() as session:
            # TODO: add in authentication
            async with session.get(app_prediction_requests_endpoint) as response:
                response.raise_for_status()
                prediction_requests = await response.json()
        
        if not prediction_requests or 'matches' not in prediction_requests:
            bt.logging.info("No app prediction requests returned from API")
            return False
        
        prediction_requests = prediction_requests['matches']
        
        bt.logging.info(f"Sending {len(prediction_requests)} app requests to miners for predictions.")
        for pr in prediction_requests:
            match_prediction = MatchPrediction(
                matchId = pr['matchId'],
                matchDate = pr['matchDate'],
                sport = pr['sport'],
                homeTeamName = pr['homeTeamName'],
                awayTeamName = pr['awayTeamName']
            )
            miner_hotkey = pr['miner_hotkey']
            if IS_DEV:
                miner_uids = [9999]
            else:
                miner_uids = [ax.uid for ax in vali.metagraph.axons if ax.hotkey == miner_hotkey]

            input_synapse = GetMatchPrediction(match_prediction=match_prediction)
            # Send prediction requests to miners and store their responses. TODO: do we need to mark the stored prediction as being an app request prediction? not sure it matters
            finished_responses, working_miner_uids = await send_predictions_to_miners(vali, input_synapse, miner_uids)

            # Post the response back per prediction_request or batch? Probably batch.

        return True

    except Exception as e:
        bt.logging.error(f"Error syncing app prediction requests: {e}")
        return False

def get_match_prediction_requests(batchsize: int = 1) -> List[MatchPrediction]:
    matches = storage.get_matches_to_predict(batchsize)
    match_predictions = [MatchPrediction(
        matchId = match.matchId,
        matchDate = str(match.matchDate),
        sport = match.sport,
        homeTeamName = match.homeTeamName,
        awayTeamName = match.awayTeamName
    ) for match in matches]
    return match_predictions

async def send_predictions_to_miners(vali: Validator, input_synapse: GetMatchPrediction, miner_uids: List[int]) -> Tuple[List[MatchPrediction], List[int]]:
    try:
        if IS_DEV:
            # For now, just return a list of random MatchPrediction responses
            responses = [
                GetMatchPrediction(
                    match_prediction=MatchPrediction(
                        matchId=input_synapse.match_prediction.matchId,
                        matchDate=input_synapse.match_prediction.matchDate,
                        sport=input_synapse.match_prediction.sport,
                        homeTeamName=input_synapse.match_prediction.homeTeamName,
                        awayTeamName=input_synapse.match_prediction.awayTeamName,
                        homeTeamScore=random.randint(0, 10),
                        awayTeamScore=random.randint(0, 10),
                    )
                ) for uid in miner_uids]
        else:
            
            random.shuffle(miner_uids)
            axons = [vali.metagraph.axons[uid] for uid in miner_uids]
            for axon in axons:
                if axon.hotkey == '5EqZoEKc6c8TaG4xRRHTT1uZiQF5jkjQCeUV5t77L6YbeaJ8':
                    axon.ip = '127.0.0.1'
            
            # convert matchDate to string for serialization
            input_synapse.match_prediction.matchDate = str(input_synapse.match_prediction.matchDate)
            responses = await vali.dendrite(
                # Send the query to selected miner axons in the network.
                axons=axons,
                synapse=input_synapse,
                deserialize=True,
                timeout=120,
            )
        
        working_miner_uids = []
        finished_responses = []
        for response in responses:
            is_prediction_valid, error_msg = is_match_prediction_valid(response.match_prediction)
            if IS_DEV:
                uid = miner_uids.pop(random.randrange(len(miner_uids)))
                working_miner_uids.append(uid)
                finished_responses.append(response)
            else:
                if response is None or response.match_prediction.homeTeamScore is None or response.match_prediction.awayTeamScore is None or response.axon is None or response.axon.hotkey is None:
                    bt.logging.info(
                        f"{response.axon.hotkey}: Miner failed to respond with a prediction."
                    )
                    continue
                elif not is_prediction_valid:
                    bt.logging.info(
                        f"{response.axon.hotkey}: Miner prediction failed validation: {error_msg}"
                    )
                    continue
                else:
                    uid = [uid for uid, axon in zip(miner_uids, axons) if axon.hotkey == response.axon.hotkey][0]
                    working_miner_uids.append(uid)
                    response.match_prediction.minerId = uid
                    response.match_prediction.hotkey = response.axon.hotkey
                    finished_responses.append(response)

        if len(working_miner_uids) == 0:
            bt.logging.info("No miner responses available.")
            return (finished_responses, working_miner_uids)
        
        bt.logging.info(f"Received responses: {responses}")
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

def find_and_score_match_predictions(batchsize: int) -> Tuple[List[float], List[int]]:
    """Query the validator's local storage for a list of qualifying MatchPredictions that can be scored.
    
    Then run scoring algorithms and return scoring results
    """

    # Query for scorable match predictions with actual match data
    predictions_with_match_data = storage.get_match_predictions_to_score(batchsize)

    rewards = []
    correct_winner_results = []
    rewards_uids = []
    predictions = []
    for pwmd in predictions_with_match_data:
        prediction = pwmd.prediction
        uid = prediction.minerId

        sport = prediction.sport
        max_score_difference = MAX_SCORE_DIFFERENCE
        if sport == Sport.SOCCER:
            max_score_difference = MAX_SCORE_DIFFERENCE_SOCCER
        elif sport == Sport.FOOTBALL:
            max_score_difference = MAX_SCORE_DIFFERENCE_FOOTBALL
        elif sport == Sport.BASEBALL:
            max_score_difference = MAX_SCORE_DIFFERENCE_BASEBALL
        elif sport == Sport.BASKETBALL:
            max_score_difference = MAX_SCORE_DIFFERENCE_BASKETBALL
        elif sport == Sport.CRICKET:
            max_score_difference = MAX_SCORE_DIFFERENCE_CRICKET

        total_score, correct_winner_score = calculate_prediction_score(
            prediction.homeTeamScore,
            prediction.awayTeamScore,
            pwmd.actualHomeTeamScore,
            pwmd.actualAwayTeamScore,
            max_score_difference
        )
        rewards.append(total_score)
        correct_winner_results.append(correct_winner_score)
        rewards_uids.append(uid)
        predictions.append(prediction)

    # mark predictions as scored in the local db
    if len(predictions) > 0:
        storage.update_match_predictions(predictions)

    # Aggregate rewards for each miner
    aggregated_rewards = defaultdict(float)
    for uid, reward in zip(rewards_uids, rewards):
        aggregated_rewards[uid] += reward

    # Convert the aggregated rewards to a list of tuples (uid, aggregated_reward)
    aggregated_rewards_list = list(aggregated_rewards.items())

    # Normalize the aggregated rewards so that they sum up to 1.0
    total_rewards = sum(reward for uid, reward in aggregated_rewards_list)
    if total_rewards > 0:
        normalized_rewards = [reward / total_rewards for uid, reward in aggregated_rewards_list]
    else:
        # Handle the case where total_rewards is 0 to avoid division by zero
        normalized_rewards = [0 for uid, reward in aggregated_rewards_list]

    # Extract the corresponding UIDs for the normalized rewards
    normalized_rewards_uids = [uid for uid, reward in aggregated_rewards_list]

    return [normalized_rewards, normalized_rewards_uids, rewards, correct_winner_results, rewards_uids]

    
def calculate_prediction_score(
        predicted_home_score: int, predicted_away_score: int,
        actual_home_score: int, actual_away_score: int,
        max_score_difference: int
    ) -> float:
    
    # Score for home team prediction
    home_score_diff = abs(predicted_home_score - actual_home_score)
    # Calculate a float score between 0 and 1. 1 being an exact match
    home_score = 0.25 - (home_score_diff / max_score_difference)
    
    # Score for away team prediction
    away_score_diff = abs(predicted_away_score - actual_away_score)
    # Calculate a float score between 0 and 1. 1 being an exact match
    away_score = 0.25 - (away_score_diff / max_score_difference)
    
    # Determine the correct winner or if it's a draw
    actual_winner = 'home' if actual_home_score > actual_away_score else 'away' if actual_home_score < actual_away_score else 'draw'
    predicted_winner = 'home' if predicted_home_score > predicted_away_score else 'away' if predicted_home_score < predicted_away_score else 'draw'
    
    # Score for correct winner prediction
    correct_winner_score = 1 if predicted_winner == actual_winner else 0
    
    # Combine the scores
    # Max score for home and away scores is 0.25. Correct match winner is 0.5. Perfectly predicted match score is 1
    total_score = home_score + away_score + (correct_winner_score * CORRECT_MATCH_WINNER_SCORE)
    
    return total_score, correct_winner_score


def is_match_prediction_valid(prediction: MatchPrediction) -> Tuple[bool, str]:
    """Performs basic validation on a MatchPrediction.

    Returns a tuple of (is_valid, reason) where is_valid is True if the entities are valid,
    and reason is a string describing why they are not valid.
    """

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

    return (True, "")


async def post_prediction_results(vali, prediction_results_endpoint, prediction_scores, correct_winner_results, prediction_rewards_uids, prediction_results_hotkeys):
    keypair = vali.dendrite.keypair
    hotkey = keypair.ss58_address
    signature = f"0x{keypair.sign(hotkey).hex()}"
    try:
        # Post the scoring results back to the api
        scoring_results = {
            'scores': prediction_scores,
            'correct_winner_results': correct_winner_results,
            'uids': prediction_rewards_uids,
            'hotkeys': prediction_results_hotkeys,
        }
        async with ClientSession() as session:
            async with session.post(
                prediction_results_endpoint,
                auth=BasicAuth(hotkey, signature),
                json=scoring_results
            ) as response:
                response.raise_for_status()
                bt.logging.info("Successfully posted prediction results to API.")

    except Exception as e:
        bt.logging.error(f"Error posting prediction results to API: {e}")


def get_single_successful_response(
    responses: List[bt.Synapse], expected_class: Type
) -> Optional[bt.Synapse]:
    """Helper function to extract the single response from a list of responses, if the response is valid.

    return: (response, is_valid): The response if it's valid, else None.
    """
    if (
        responses
        and isinstance(responses, list)
        and len(responses) == 1
        and isinstance(responses[0], expected_class)
        and responses[0].is_success
    ):
        return responses[0]
    return None


def get_match_prediction_from_response(response: GetMatchPrediction) -> GetMatchPrediction:
    """Gets a MatchPrediction from a GetMatchPrediction response."""
    assert response.is_success

    if not response.match_prediction:
        raise ValueError("GetMatchPrediction response has no MatchPrediction.")

    return response.match_prediction


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
        uids = random.sample(
            available_uids,
            min(k, len(available_uids))
        )
    else:
        uids = available_uids

    return uids
