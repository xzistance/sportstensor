import contextlib
import datetime as dt
import time
import bittensor as bt
import sqlite3
import threading
import random
from pydantic import ValidationError
from typing import Any, Dict, Optional, Set, Tuple, List, Union
from common.data import (
    Match,
    Player,
    MatchPrediction,
    PlayerStat,
    Stat,
    PlayerPrediction,
    PlayerEligibleStat,
    League,
    MatchPredictionWithMatchData,
)
from common.protocol import GetMatchPrediction, GetPlayerPrediction
from common.constants import (
    IS_DEV,
    MIN_PREDICTION_TIME_THRESHOLD,
    MAX_PREDICTION_DAYS_THRESHOLD,
    SCORING_CUTOFF_IN_DAYS,
)
from storage.validator_storage import ValidatorStorage


class SqliteValidatorStorage(ValidatorStorage):
    """Sqlite in-memory backed Validator Storage"""

    LEAGUES_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Leagues (
                            leagueId      VARCHAR(50)     PRIMARY KEY,
                            leagueName    VARCHAR(50)     NOT NULL,
                            sport         INTEGER         NOT NULL,
                            isActive      INTEGER         DEFAULT 0,
                            lastUpdated   TIMESTAMP(6)    NOT NULL
                            )"""

    MATCHES_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Matches (
                            matchId       VARCHAR(50)     PRIMARY KEY,
                            matchDate     TIMESTAMP(6)    NOT NULL,
                            sport         INTEGER         NOT NULL,
                            league        VARCHAR(50)     NOT NULL,
                            homeTeamName  VARCHAR(30)     NOT NULL,
                            awayTeamName  VARCHAR(30)     NOT NULL,
                            homeTeamScore INTEGER         NULL,
                            awayTeamScore INTEGER         NULL,
                            isComplete    INTEGER         DEFAULT 0,
                            lastUpdated   TIMESTAMP(6)    NOT NULL
                            )"""

    MATCHPREDICTIONS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS MatchPredictions (
                            predictionId  INTEGER         PRIMARY KEY,
                            minerId       INTEGER         NOT NULL,
                            hotkey        VARCHAR(64)     NOT NULL,
                            matchId       VARCHAR(50)     NOT NULL,
                            matchDate     TIMESTAMP(6)    NOT NULL,
                            sport         INTEGER         NOT NULL,
                            league        VARCHAR(50)     NOT NULL,
                            homeTeamName  VARCHAR(30)     NOT NULL,
                            awayTeamName  VARCHAR(30)     NOT NULL,
                            homeTeamScore INTEGER         NULL,
                            awayTeamScore INTEGER         NULL,
                            isScored      INTEGER         DEFAULT 0,
                            scoredDate    TIMESTAMP(6)    NULL,
                            lastUpdated   TIMESTAMP(6)    NOT NULL
                            )"""
    
    STATS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Stats (
                            statId              VARCHAR(50)     PRIMARY KEY,
                            statName            VARCHAR(30)     NOT NULL,
                            statAbbr            VARCHAR(10)     NULL,
                            statDescription     VARCHAR(100)    NULL,
                            statType            VARCHAR(30)     NOT NULL,
                            sport               INTEGER         NOT NULL
                            )"""
    
    PLAYERS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS Players (
                            playerId            VARCHAR(50)     PRIMARY KEY,
                            playerName          VARCHAR(30)     NOT NULL,
                            playerTeam          VARCHAR(30)     NOT NULL,
                            playerPosition      VARCHAR(30)     NULL,
                            sport               INTEGER         NOT NULL,
                            league              VARCHAR(50)     NOT NULL,
                            stats               VARCHAR(30)     NOT NULL
                            )"""
    
    PLAYER_ELIGIBLE_STATS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS PlayerEligibleStats (
                            playerId            VARCHAR(50)         NOT NULL,
                            statId              VARCHAR(50)         NOT NULL,
                            PRIMARY KEY (playerId, statId),
                            FOREIGN KEY (playerId) REFERENCES Players(playerId),
                            FOREIGN KEY (statId) REFERENCES Stats(statId)
                            )"""
    
    PLAYERMATCHSTATS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS PlayerMatchStats (
                            playerStatId    INTEGER         PRIMARY KEY,
                            matchId         VARCHAR(50)     NOT NULL,
                            playerId        INTEGER         NOT NULL,
                            statId          INTEGER         NOT NULL,
                            statValue       VARCHAR(30)     NULL,
                            statValueType   VARCHAR(10)     NULL,
                            lastUpdated     TIMESTAMP(6)    NOT NULL
                            )"""
    
    PLAYERPREDICTIONS_TABLE_CREATE = """CREATE TABLE IF NOT EXISTS PlayerPredictions (
                            predictionId    INTEGER         PRIMARY KEY,
                            minerId         INTEGER         NOT NULL,
                            hotkey          VARCHAR(64)     NOT NULL,
                            matchId         VARCHAR(50)     NOT NULL,
                            matchDate       TIMESTAMP(6)    NOT NULL,
                            sport           INTEGER         NOT NULL,
                            league          VARCHAR(50)     NOT NULL,
                            playerName      VARCHAR(30)     NOT NULL,
                            playerTeam      VARCHAR(30)     NOT NULL,
                            playerPosition  VARCHAR(30)     NULL,
                            statName        VARCHAR(30)     NOT NULL,
                            statAbbr        VARCHAR(10)     NULL,
                            statDescription VARCHAR(100)    NULL,
                            statType        VARCHAR(30)     NOT NULL,
                            statValue       VARCHAR(30)     NOT NULL,
                            statValueType   VARCHAR(10)     NOT NULL,
                            isScored        INTEGER         DEFAULT 0,
                            scoredDate      TIMESTAMP(6)    NULL,
                            lastUpdated     TIMESTAMP(6)    NOT NULL
                            )"""

    def __init__(self):
        sqlite3.register_converter("timestamp", tz_aware_timestamp_adapter)

        self.continuous_connection_do_not_reuse = self._create_connection()

        with contextlib.closing(self._create_connection()) as connection:
            cursor = connection.cursor()

            # Create the Matches table (if it does not already exist).
            # cursor.execute(SqliteValidatorStorage.LEAGUES_TABLE_CREATE)

            # Create the Matches table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.MATCHES_TABLE_CREATE)

            # Create the MatchPredictions table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.MATCHPREDICTIONS_TABLE_CREATE)

            # Create the Stats table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.STATS_TABLE_CREATE)

            # Create the Players table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.PLAYERS_TABLE_CREATE)

            # Create the PlayerEligibleStats table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.PLAYER_ELIGIBLE_STATS_TABLE_CREATE)

            # Create the PlayerMatchStats table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.PLAYERMATCHSTATS_TABLE_CREATE)

            # Create the PlayerPredictions table (if it does not already exist).
            cursor.execute(SqliteValidatorStorage.PLAYERPREDICTIONS_TABLE_CREATE)

            # Lock to avoid concurrency issues on interacting with the database.
            self.lock = threading.RLock()

        # Execute cleanup queries
        self.cleanup()

    def _create_connection(self):
        # Create the database if it doesn't exist, defaulting to the local directory.
        # Use PARSE_DECLTYPES to convert accessed values into the appropriate type.
        connection = sqlite3.connect(
            "SportsTensor.db",
            uri=True,
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=120.0,
        )
        # Avoid using a row_factory that would allow parsing results by column name for performance.
        # connection.row_factory = sqlite3.Row
        connection.isolation_level = None
        return connection
    
    def cleanup(self):
        """Cleanup the database."""
        print("Cleaning up the database")
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                # Execute cleanup queries

                # Clean up bug where we accidentally inserted 'REDACTED' as scores
                cursor.execute(
                    """DELETE FROM MatchPredictions WHERE homeTeamScore='REDACTED' OR awayTeamScore='REDACTED'"""
                )
                connection.commit()

    def insert_leagues(self, leagues: List[League]):
        """Stores leagues associated with sports. Indicates which leagues are active to run predictions on."""
        values = []
        for league in leagues:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            # Parse every League into a list of values to insert.
            values.append(
                [
                    league.leagueId,
                    league.leagueName,
                    league.sport,
                    league.isActive,
                    now_str,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO Leagues (leagueId, leagueName, sport, isActive, lastUpdated) VALUES (?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()

    def update_leagues(self, leagues: List[League]):
        """Updates leagues. Mainly for activating or deactivating"""
        values = []
        for league in leagues:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse Leagues into a list of values to update.
            values.append(
                [league.leagueName, league.isActive, now_str, league.leagueId]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """UPDATE Leagues SET leagueName = ?, isActive = ?, lastUpdated = ? WHERE leagueId = ?""",
                    values,
                )
                connection.commit()

    def insert_matches(self, matches: List[Match]):
        """Stores official matches to score predictions from miners on."""
        values = []
        for match in matches:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse every Match into a list of values to insert.
            values.append(
                [
                    match.matchId,
                    match.matchDate,
                    match.sport,
                    match.league,
                    match.homeTeamName,
                    match.awayTeamName,
                    match.homeTeamScore,
                    match.awayTeamScore,
                    match.isComplete,
                    now_str,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO Matches (matchId, matchDate, sport, league, homeTeamName, awayTeamName, homeTeamScore, awayTeamScore, isComplete, lastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()

    def update_matches(self, matches: List[Match]):
        """Updates matches. Typically only used when updating final score."""
        values = []
        for match in matches:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse Matches into a list of values to update.
            values.append(
                [
                    match.homeTeamScore,
                    match.awayTeamScore,
                    match.isComplete,
                    now_str,
                    match.matchId,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """UPDATE Matches SET homeTeamScore = ?, awayTeamScore = ?, isComplete = ?, lastUpdated = ? WHERE matchId = ?""",
                    values,
                )
                connection.commit()

    def check_match(self, matchId: str) -> Match:
        """Check if a match with the given ID exists in the database."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT EXISTS(SELECT 1 FROM matches WHERE matchId = ?)""",
                    (matchId,),
                )
                return cursor.fetchone()[0]
            
    def get_match(self, matchId: str) -> Match:
        """Gets a match with the given ID from the database."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT * FROM Matches WHERE matchId = ?""",
                    (matchId,),
                )
                result = cursor.fetchone()
                if result is not None:
                    return Match(
                        **dict(zip([column[0] for column in cursor.description], result))
                    )
                else:
                    return None

    def check_stats(self, statId: str) -> Stat:
        """Check if a stat with the given ID exists in the database."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT EXISTS(SELECT 1 FROM stats WHERE statId = ?)""",
                    (statId,),
                )
                return cursor.fetchone()[0]

    def insert_stats(self, stats: List[Stat]):
        """Stores stats """
        values = []
        for stat in stats:
            # Parse every Match into a list of values to insert.
            values.append(
                [
                    stat.statId,
                    stat.statName,
                    stat.statAbbr,
                    stat.statDescription,
                    stat.statType,
                    stat.sport
                ]
            )
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO Stats (statId, statName, statAbbr, statDescription, statType, sport) VALUES (?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()
    
    def check_player(self, playerId: str) -> Player:
        """Check if a player with the given ID exists in the database."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT EXISTS(SELECT 1 FROM Players WHERE playerId = ?)""",
                    (playerId,),
                )
                return cursor.fetchone()[0]

    def insert_players(self, players: List[Player]):
        """Stores players """
        values = []
        for player in players:

            # Parse every Match into a list of values to insert.
            values.append(
                [
                    player.playerId,
                    player.playerName,
                    player.playerTeam,
                    player.playerPosition,
                    player.sport,
                    player.league,
                    player.stats,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO Players (playerId, playerName, playerTeam, playerPosition, sport, league, stats) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()
    
    def update_players(self, players: List[Player]):
        """Updates players."""
        values = []
        for player in players:
            # Parse Matches into a list of values to update.
            values.append(
                [
                    player.playerTeam,
                    player.playerPosition,
                    player.stats,
                    player.playerId
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """UPDATE Players SET playerTeam = ?, playerPosition = ?, stats = ? WHERE playerId = ?""",
                    values,
                )
                connection.commit()
    
    def insert_player_eligible_stats(self, playerEligibleStats: List[PlayerEligibleStat]):
        """Stores players """
        values = []
        for pes in playerEligibleStats:

            # Parse every Match into a list of values to insert.
            values.append(
                [
                    pes.playerId,
                    pes.statId,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO PlayerEligibleStats (playerId, statId) VALUES (?, ?)""",
                    values,
                )
                connection.commit()
        
    def update_player_eligible_stats(self, playerEligibleStats: List[PlayerEligibleStat]):
        """Update players """
        values = []

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                for pes in playerEligibleStats:
                    # Delete existing stats for the player
                    cursor.execute(
                        """DELETE FROM PlayerEligibleStats WHERE playerId = ?""",
                        (pes.playerId,)
                    )
                    # Parse every Match into a list of values to insert.
                    values.append(
                        [
                            pes.playerId,
                            pes.statId,
                        ]
                    )
                cursor.executemany(
                    """INSERT OR IGNORE INTO PlayerEligibleStats (playerId, statId) VALUES (?, ?)""",
                    values,
                )
                connection.commit()

    def get_matches_to_predict(self, batchsize: int = 10) -> List[Match]:
        """Gets batchsize number of matches ready to be predicted."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:

                # Calculate the current timestamp
                current_timestamp = int(time.time())
                # Calculate the lower bound timestamp (earliest match date allowed for predictions)
                lower_bound_timestamp = (
                    current_timestamp + MIN_PREDICTION_TIME_THRESHOLD
                )
                # Calculate the upper bound timestamp (latest match date allowed for predictions)
                upper_bound_timestamp = (
                    current_timestamp + MAX_PREDICTION_DAYS_THRESHOLD * 24 * 3600
                )
                # Convert timestamps to strings in 'YYYY-MM-DD HH:MM:SS' format
                lower_bound_str = dt.datetime.utcfromtimestamp(
                    lower_bound_timestamp
                ).strftime("%Y-%m-%d %H:%M:%S")
                upper_bound_str = dt.datetime.utcfromtimestamp(
                    upper_bound_timestamp
                ).strftime("%Y-%m-%d %H:%M:%S")

                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT * 
                    FROM Matches
                    WHERE isComplete = 0
                    AND matchDate BETWEEN ? AND ?
                    ORDER BY RANDOM() LIMIT ?
                    """,
                    (lower_bound_str, upper_bound_str, batchsize),
                )
                # print("matchDate > ", lower_bound_str, "matchDate < ", upper_bound_str)
                results = cursor.fetchall()
                if not results:
                    return []

                # Convert the raw database results into Pydantic models
                matches = [
                    Match(
                        **dict(zip([column[0] for column in cursor.description], row))
                    )
                    for row in results
                ]
                return matches

    def get_players_to_predict(self, match: MatchPrediction, batchsize: int) -> List[Player]:
        """Gets batchsize number of players eligible to be predicted."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                # Get players from home team
                cursor.execute(
                    """
                    SELECT * 
                    FROM Players
                    WHERE playerTeam = ?
                    ORDER BY RANDOM() LIMIT ?
                    """,
                    (match.homeTeamName, batchsize),
                )
                home_players = cursor.fetchall()

                # Get players from away team
                cursor.execute(
                    """
                    SELECT * 
                    FROM Players
                    WHERE playerTeam = ?
                    ORDER BY RANDOM() LIMIT ?
                    """,
                    (match.awayTeamName, batchsize),
                )
                away_players = cursor.fetchall()

                results = home_players + away_players

                if not results:
                    return []

                # Convert the raw database results into Pydantic models
                players = [
                    Player(
                        **dict(zip([column[0] for column in cursor.description], row))
                    )
                    for row in results
                ]
                return players

    def get_eligible_player_stats(self, player: Player, batchsize: int) -> List[Stat]:
        """Gets a list of stats that a player is eligible to be predicted on."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """
                    SELECT s.* 
                    FROM Stats s
                    JOIN PlayerEligibleStats pes ON s.statId = pes.statId
                    WHERE pes.playerId = ?
                    AND s.sport = ?
                    ORDER BY RANDOM() LIMIT ?
                    """,
                    (player.playerId, player.sport, batchsize),
                )
                results = cursor.fetchall()
                if not results:
                    return []

                # Convert the raw database results into Pydantic models
                stats = [
                    Stat(
                        **dict(zip([column[0] for column in cursor.description], row))
                    )
                    for row in results
                ]
                return stats
    
    def insert_match_predictions(self, predictions: List[GetMatchPrediction]):
        """Stores unscored match predictions returned from miners."""
        values = []
        for prediction in predictions:
            """
            bt.logging.debug(f" \
                    [{prediction.match_prediction.minerId}] {prediction.match_prediction.hotkey}: Upserting prediction for match {str(prediction.match_prediction.matchId)}, \
                    {prediction.match_prediction.awayTeamName} at {prediction.match_prediction.homeTeamName} \
                    on {str(prediction.match_prediction.matchDate)} \
            ")
            """

            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            if IS_DEV:
                random_uid = random.randint(
                    1, 16
                )  # Generate a random integer between 1 and 16

            # Parse every MatchPrediction into a list of values to insert.
            values.append(
                [
                    prediction.match_prediction.minerId if not IS_DEV else random_uid,
                    (
                        prediction.match_prediction.hotkey
                        if not IS_DEV
                        else f"DEV_{str(random_uid)}"
                    ),
                    prediction.match_prediction.matchId,
                    prediction.match_prediction.matchDate,
                    prediction.match_prediction.sport,
                    prediction.match_prediction.league,
                    prediction.match_prediction.homeTeamName,
                    prediction.match_prediction.awayTeamName,
                    prediction.match_prediction.homeTeamScore,
                    prediction.match_prediction.awayTeamScore,
                    now_str,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO MatchPredictions (minerId, hotkey, matchId, matchDate, sport, league, homeTeamName, awayTeamName, homeTeamScore, awayTeamScore, lastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()
    
    def insert_player_predictions(self, predictions: List[list[GetPlayerPrediction]]):
        """Stores unscored player predictions returned from miners."""
        values = []
        for prediction in predictions:

            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            if IS_DEV:
                random_uid = random.randint(
                    1, 16
                )  # Generate a random integer between 1 and 16

            # Parse every MatchPrediction into a list of values to insert.
            for player_prediction in prediction:
                values.append(
                    [
                        player_prediction.player_prediction.minerId if not IS_DEV else random_uid,
                        (
                            player_prediction.player_prediction.hotkey
                            if not IS_DEV
                            else f"DEV_{str(random_uid)}"
                        ),
                        player_prediction.player_prediction.matchId,
                        player_prediction.player_prediction.matchDate,
                        player_prediction.player_prediction.sport,
                        player_prediction.player_prediction.league,
                        player_prediction.player_prediction.playerName,
                        player_prediction.player_prediction.playerTeam,
                        player_prediction.player_prediction.playerPosition,
                        player_prediction.player_prediction.statName,
                        player_prediction.player_prediction.statAbbr,
                        player_prediction.player_prediction.statDescription,
                        player_prediction.player_prediction.statType,
                        player_prediction.player_prediction.statValue,
                        now_str,
                    ]
                )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO PlayerPredictions (minerId, hotkey, matchId, matchDate, sport, league, playerName, playerTeam, playerPosition, statName, statAbbr, statDescription, statType, statValue, lastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()

    def get_match_predictions_to_score(
        self, batchsize: int = 10, matchDateCutoff: int = SCORING_CUTOFF_IN_DAYS
    ) -> Optional[List[MatchPredictionWithMatchData]]:
        """Gets batchsize number of predictions that need to be scored and are eligible to be scored (the match is complete)"""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()

                # Calculate the current timestamp
                current_timestamp = int(time.time())
                # Calculate cutoff date timestamp
                match_cutoff_timestamp = current_timestamp - (
                    matchDateCutoff * 24 * 3600
                )
                # Convert timestamps to strings in 'YYYY-MM-DD HH:MM:SS' format
                match_cutoff_str = dt.datetime.utcfromtimestamp(
                    match_cutoff_timestamp
                ).strftime("%Y-%m-%d %H:%M:%S")

                cursor.execute(
                    """
                    SELECT mp.*, m.homeTeamScore as actualHomeTeamScore, m.awayTeamScore as actualAwayTeamScore
                    FROM MatchPredictions mp
                    JOIN Matches m ON (m.matchId = mp.matchId)
                    WHERE mp.isScored = 0
                    AND m.isComplete = 1
                    AND mp.matchDate > ?
                    AND mp.homeTeamScore IS NOT NULL
                    AND mp.awayTeamScore IS NOT NULL
                    AND m.homeTeamScore IS NOT NULL
                    AND m.awayTeamScore IS NOT NULL
                    ORDER BY mp.matchDate ASC
                    LIMIT ?
                    """,
                    [match_cutoff_str, batchsize],
                )
                results = cursor.fetchall()
                if not results:
                    return []

                # Convert the raw database results into the new combined Pydantic model
                combined_predictions = []
                for row in results:
                    prediction_data = {
                        "predictionId": row[0],
                        "minerId": row[1],
                        "hotkey": row[2],
                        "matchId": row[3],
                        "matchDate": row[4],
                        "sport": row[5],
                        "league": row[6],
                        "homeTeamName": row[7],
                        "awayTeamName": row[8],
                        "homeTeamScore": row[9],
                        "awayTeamScore": row[10],
                        "isScored": row[11],
                        # row[12] is scoredDate
                        # row[13] is lastUpdated
                    }
                    try:
                        combined_predictions.append(
                            MatchPredictionWithMatchData(
                                prediction=MatchPrediction(**prediction_data),
                                actualHomeTeamScore=row[14],
                                actualAwayTeamScore=row[15],
                            )
                        )
                    except ValidationError as e:
                        bt.logging.error(f"Validation error for row {row}: {e}")

                return combined_predictions

    def update_match_predictions(self, predictions: List[MatchPrediction]):
        """Updates predictions. Typically only used when marking predictions as being scored."""
        values = []
        for prediction in predictions:
            bt.logging.trace(
                f"{prediction.hotkey}: Marking prediction {str(prediction.predictionId)} as scored"
            )

            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse MatchPredictions into a list of values to update, marking each as scored with a timestamp of now.
            values.append([1, now_str, prediction.predictionId])

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """UPDATE MatchPredictions SET isScored = ?, scoredDate = ? WHERE predictionId = ?""",
                    values,
                )
                connection.commit()

    def get_miner_match_predictions(
        self, miner_hotkey: str, scored=False
    ) -> Optional[List[MatchPrediction]]:
        """Gets a list of all predictions made by a miner."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT * FROM MatchPredictions WHERE hotkey = ?",
                    [miner_hotkey],
                )
                results = cursor.fetchall()
                if not results:
                    return []

                # Convert the raw database results into Pydantic models
                predictions = [
                    MatchPrediction(
                        **dict(zip([column[0] for column in cursor.description], row))
                    )
                    for row in results
                ]
                return predictions

    def insert_player_stats(self, stats: List[PlayerStat]):
        """Stores player stats to score predictions from miners on."""
        values = []
        for stat in stats:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse every PlayerStat into a list of values to insert.
            values.append(
                [
                    stat.playerStatId,
                    stat.matchId,
                    stat.playerName,
                    stat.playerTeam,
                    stat.playerPosition,
                    stat.statType,
                    stat.statValue,
                    now_str,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """INSERT OR IGNORE INTO PlayerMatchStats (playerStatId, matchId, playerName, playerTeam, playerPosition, statType, statValue, lastUpdated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                connection.commit()

    def update_player_stats(self, stats: List[PlayerStat]):
        """Updates player stats. Typically only used when updating final stats."""
        values = []
        for stat in stats:
            now_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Parse PlayerStats into a list of values to update.
            values.append(
                [
                    stat.statValue,
                    now_str,
                    stat.playerStatId,
                ]
            )

        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.executemany(
                    """UPDATE PlayerMatchStats SET statValue = ?, lastUpdated = ? WHERE playerStatId = ?""",
                    values,
                )
                connection.commit()

    def check_player_stat(self, playerStatId: str) -> PlayerStat:
        """Check if a player stat with the given ID exists in the database."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    """SELECT EXISTS(SELECT 1 FROM PlayerMatchStats WHERE playerStatId = ?)""",
                    (playerStatId,),
                )
                return cursor.fetchone()[0]

    def read_miner_last_prediction(self, miner_hotkey: str) -> Optional[dt.datetime]:
        """Gets when a specific miner last returned a prediction."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "SELECT MAX(lastUpdated) FROM MatchPrediction WHERE hotkey = ?",
                    [miner_hotkey],
                )
                result = cursor.fetchone()
                if result is not None:
                    return result[0]
                else:
                    return None

    def delete_miner(self, hotkey: str):
        """Removes the predictions and miner information for the specified miner."""
        with self.lock:
            with contextlib.closing(self._create_connection()) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "DELETE FROM MatchPredictions WHERE hotkey = ?", [hotkey]
                )


# Use a timezone aware adapter for timestamp columns.
def tz_aware_timestamp_adapter(val):
    datepart, timepart = val.split(b" ")
    year, month, day = map(int, datepart.split(b"-"))

    if b"+" in timepart:
        timepart, tz_offset = timepart.rsplit(b"+", 1)
        if tz_offset == b"00:00":
            tzinfo = dt.timezone.utc
        else:
            hours, minutes = map(int, tz_offset.split(b":", 1))
            tzinfo = dt.timezone(dt.timedelta(hours=hours, minutes=minutes))
    elif b"-" in timepart:
        timepart, tz_offset = timepart.rsplit(b"-", 1)
        if tz_offset == b"00:00":
            tzinfo = dt.timezone.utc
        else:
            hours, minutes = map(int, tz_offset.split(b":", 1))
            tzinfo = dt.timezone(dt.timedelta(hours=-hours, minutes=-minutes))
    else:
        tzinfo = None

    timepart_full = timepart.split(b".")
    hours, minutes, seconds = map(int, timepart_full[0].split(b":"))

    if len(timepart_full) == 2:
        microseconds = int("{:0<6.6}".format(timepart_full[1].decode()))
    else:
        microseconds = 0

    val = dt.datetime(year, month, day, hours, minutes, seconds, microseconds, tzinfo)

    return val
