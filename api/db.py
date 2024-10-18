import mysql.connector
import logging
import datetime as dt
from datetime import timezone
from api.config import IS_PROD, DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
import os
import time

def generate_uuid():
    return os.urandom(16).hex()

GET_MATCH_QUERY = """
    SELECT
        mlo.*,
        mo.homeTeamOdds,
        mo.awayTeamOdds,
        mo.drawOdds,
        mo.lastUpdated,
        (SELECT COUNT(*) FROM match_odds mo WHERE mlo.oddsapiMatchId = mo.oddsapiMatchId) AS odds_count
    FROM (
        SELECT
            m.matchId,
            m.matchDate,
            m.homeTeamName,
            m.awayTeamName,
            m.sport,
            CASE
                WHEN m.isComplete = 1 THEN COALESCE(m.homeTeamScore, 0)
                ELSE m.homeTeamScore 
            END AS homeTeamScore,
            CASE 
                WHEN m.isComplete = 1 THEN COALESCE(m.awayTeamScore, 0)
                ELSE m.awayTeamScore 
            END AS awayTeamScore,
            m.matchLeague,
            m.isComplete,
            ml.oddsapiMatchId
        FROM matches m
        LEFT JOIN matches_lookup ml ON m.matchId = ml.matchId
    ) mlo
    LEFT JOIN (
        SELECT id, oddsapiMatchId, homeTeamOdds, awayTeamOdds, drawOdds, lastUpdated
        FROM match_odds
        WHERE (oddsapiMatchId, lastUpdated) IN (
            SELECT oddsapiMatchId, MAX(lastUpdated)
            FROM match_odds
            GROUP BY oddsapiMatchId 
        )
    ) mo ON mlo.oddsapiMatchId = mo.oddsapiMatchId
"""

def match_id_exists(match_id):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM matches WHERE matchId = %s", (match_id,))
        count = cursor.fetchone()[0]

        return count > 0

    except Exception as e:
        logging.error("Failed to check if match exists in MySQL database", exc_info=True)
        return False
    finally:
        cursor.close()
        conn.close()

def match_odds_id_exists(match_odds_id):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM match_odds WHERE id = %s", (match_odds_id,))
        count = cursor.fetchone()[0]

        return count > 0

    except Exception as e:
        logging.error("Failed to check if match_odds exists in MySQL database", exc_info=True)
        return False
    finally:
        cursor.close()
        conn.close()

def get_matches(all=False):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SET @current_time_utc = CONVERT_TZ(NOW(), @@session.time_zone, '+00:00')")
        query = GET_MATCH_QUERY

        if not all:
            query += """
                WHERE mlo.matchDate BETWEEN @current_time_utc - INTERVAL 10 DAY AND @current_time_utc + INTERVAL 48 HOUR
            """
        
        cursor.execute(query)
        match_list = cursor.fetchall()

        return match_list

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_upcoming_matches():
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        # Set the current time in UTC
        cursor.execute("SET @current_time_utc = CONVERT_TZ(NOW(), @@session.time_zone, '+00:00')")

        query = GET_MATCH_QUERY + """
           WHERE mlo.matchDate BETWEEN @current_time_utc AND @current_time_utc + INTERVAL 48 HOUR
        """

        cursor.execute(query)
        match_list = cursor.fetchall()

        return match_list

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_matches_with_no_odds():
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT ml.*, m.*
            FROM matches_lookup ml
            LEFT JOIN matches m
            ON ml.matchId = m.matchId
        """

        cursor.execute(query)
        matches = cursor.fetchall()

        return matches

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_stored_odds(lastUpdated = None):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        if lastUpdated:
            query = """
                SELECT mo.*, o.homeTeamName, o.awayTeamName, o.commence_time, o.league
                FROM (
                    SELECT id, oddsapiMatchId, homeTeamOdds, awayTeamOdds, drawOdds, lastUpdated
                    FROM match_odds
                    WHERE (oddsapiMatchId, lastUpdated) IN (
                        SELECT oddsapiMatchId, MAX(lastUpdated)
                        FROM match_odds
                        WHERE lastUpdated < %s
                        GROUP BY oddsapiMatchId 
                    )
                ) mo
                INNER JOIN odds o
                ON mo.oddsapiMatchId = o.oddsapiMatchId
            """
            cursor.execute(query, (lastUpdated,))
            stored_odds = cursor.fetchall()
        else:
            query = """
                SELECT mo.*, o.homeTeamName, o.awayTeamName, o.commence_time, o.league
                FROM (
                    SELECT id, oddsapiMatchId, homeTeamOdds, awayTeamOdds, drawOdds, lastUpdated
                    FROM match_odds
                    WHERE (oddsapiMatchId, lastUpdated) IN (
                        SELECT oddsapiMatchId, MAX(lastUpdated)
                        FROM match_odds
                        GROUP BY oddsapiMatchId 
                    )
                ) mo
                INNER JOIN odds o
                ON mo.oddsapiMatchId = o.oddsapiMatchId
            """
            cursor.execute(query)
            stored_odds = cursor.fetchall()

        return stored_odds

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_match_odds_by_id(match_id):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        if match_id:
            query = """
                SELECT mo.*, ml.matchId
                FROM match_odds mo
                LEFT JOIN matches_lookup ml ON mo.oddsapiMatchId = ml.oddsapiMatchId
                WHERE ml.matchId = %s
                ORDER BY mo.lastUpdated ASC
            """
            cursor.execute(query, (match_id,))
            match_odds = cursor.fetchall()
        else:
            query = """
                SELECT
                    mlo.*,
                    (
                        SELECT
                            JSON_ARRAYAGG(
                                JSON_OBJECT(
                                    "oddsapiMatchId", mo.oddsapiMatchId,
                                    "homeTeamOdds", mo.homeTeamOdds,
                                    "awayTeamOdds", mo.awayTeamOdds,
                                    "drawOdds", mo.drawOdds,
                                    "lastUpdated", mo.lastUpdated
                                )
                            )
                        FROM match_odds mo
                        WHERE mlo.oddsapiMatchId = mo.oddsapiMatchId
                    ) AS oddsData
                FROM (
                    SELECT
                        m.matchId,
                        m.matchDate,
                        m.homeTeamName,
                        m.awayTeamName,
                        m.sport,
                        CASE 
                            WHEN m.isComplete = 1 THEN COALESCE(m.homeTeamScore, 0)
                            ELSE m.homeTeamScore 
                        END AS homeTeamScore,
                        CASE 
                            WHEN m.isComplete = 1 THEN COALESCE(m.awayTeamScore, 0)
                            ELSE m.awayTeamScore 
                        END AS awayTeamScore,
                        m.matchLeague,
                        m.isComplete,
                        ml.oddsapiMatchId
                    FROM matches m
                    LEFT JOIN matches_lookup ml ON m.matchId = ml.matchId
                ) mlo
            """
            cursor.execute(query)
            match_odds = cursor.fetchall()
        
        return match_odds

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_matches_with_missing_odds():
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)
        # Set the current time in UTC
        cursor.execute("SET @current_time_utc = CONVERT_TZ(NOW(), @@session.time_zone, '+00:00')")

        query = """
            SELECT
                mlo.*,
                (
                    SELECT
                        JSON_ARRAYAGG(
                            JSON_OBJECT(
                                "oddsapiMatchId", mo.oddsapiMatchId,
                                "homeTeamOdds", mo.homeTeamOdds,
                                "awayTeamOdds", mo.awayTeamOdds,
                                "drawOdds", mo.drawOdds,
                                "lastUpdated", mo.lastUpdated
                            )
                        )
                    FROM match_odds mo
                    WHERE mlo.oddsapiMatchId = mo.oddsapiMatchId
                    ORDER BY mo.lastUpdated ASC
                ) AS oddsData
            FROM (
                SELECT
                    m.matchId,
                    m.matchDate,
                    m.homeTeamName,
                    m.awayTeamName,
                    m.sport,
                    CASE 
                        WHEN m.isComplete = 1 THEN COALESCE(m.homeTeamScore, 0)
                        ELSE m.homeTeamScore 
                    END AS homeTeamScore,
                    CASE 
                        WHEN m.isComplete = 1 THEN COALESCE(m.awayTeamScore, 0)
                        ELSE m.awayTeamScore 
                    END AS awayTeamScore,
                    m.matchLeague,
                    m.isComplete,
                    ml.oddsapiMatchId
                FROM matches m
                LEFT JOIN matches_lookup ml ON m.matchId = ml.matchId
                WHERE m.matchDate BETWEEN @current_time_utc - INTERVAL 10 DAY AND @current_time_utc AND m.isComplete = 1 
            ) mlo
        """

        cursor.execute(query)
        matches = cursor.fetchall()

        return matches

    except Exception as e:
        logging.error(
            "Failed to retrieve matches from the MySQL database", exc_info=True
        )
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

def get_match_by_id(match_id):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        query = GET_MATCH_QUERY + """
            WHERE mlo.matchId = %s
        """
        cursor.execute(query, (match_id,))
        match = cursor.fetchone()

        return match

    except Exception as e:
        logging.error("Failed to retrieve match from the MySQL database", exc_info=True)
        return False
    finally:
        cursor.close()
        conn.close()


def insert_match(match_id, event, sport_type, is_complete, current_utc_time):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO matches (matchId, matchDate, sport, homeTeamName, awayTeamName, homeTeamScore, awayTeamScore, matchLeague, isComplete, lastUpdated) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE 
                matchDate=VALUES(matchDate),
                sport=VALUES(sport),
                homeTeamName=VALUES(homeTeamName),
                awayTeamName=VALUES(awayTeamName),
                homeTeamScore=CASE 
                    WHEN VALUES(isComplete) = 1 THEN COALESCE(VALUES(homeTeamScore), 0)
                    ELSE VALUES(homeTeamScore)
                END,
                awayTeamScore=CASE 
                    WHEN VALUES(isComplete) = 1 THEN COALESCE(VALUES(awayTeamScore), 0)
                    ELSE VALUES(awayTeamScore)
                END,
                matchLeague=VALUES(matchLeague),
                isComplete=VALUES(isComplete),
                lastUpdated=VALUES(lastUpdated)
            """,
            (
                match_id,
                event.get("strTimestamp"),
                sport_type,
                event.get("strHomeTeam"),
                event.get("strAwayTeam"),
                event.get("intHomeScore"),
                event.get("intAwayScore"),
                event.get("strLeague"),
                is_complete,
                current_utc_time,
            ),
        )

        conn.commit()
        logging.info("Data inserted or updated in database")
        return True

    except Exception as e:
        logging.error("Failed to insert match in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def insert_sportsdb_match_lookup(match_id, sportsdb_match_id):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """
            INSERT IGNORE INTO matches_lookup (matchId, sportsdbMatchId) 
            VALUES (%s, %s)
            """,
            (
                match_id,
                sportsdb_match_id
            ),
        )

        conn.commit()
        logging.info("Match lookup inserted in database")
        return True

    except Exception as e:
        logging.error("Failed to insert match lookup in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def query_sportsdb_match_lookup(sportsdb_match_id):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT matchId FROM matches_lookup WHERE sportsdbMatchId = %s",
            (sportsdb_match_id,),
        )
        match_id = cursor.fetchone()

        return match_id[0] if match_id else None

    except Exception as e:
        logging.error("Failed to query sportsdb match lookup in MySQL database", exc_info=True)
        return None
    finally:
        cursor.close()
        conn.close()

def insert_match_odds_bulk(match_data):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.executemany(
            """
            INSERT IGNORE INTO match_odds (id, oddsapiMatchId, homeTeamOdds, awayTeamOdds, drawOdds, lastUpdated) 
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            match_data,
        )

        conn.commit()
        return True

    except Exception as e:
        logging.error("Failed to insert match lookup in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def insert_match_lookups_bulk(match_lookup_data):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.executemany(
            """
            INSERT IGNORE INTO matches_lookup (matchId, oddsapiMatchId) 
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE
                oddsapiMatchId=VALUES(oddsapiMatchId)
            """,
            match_lookup_data,
        )

        conn.commit()
        return True

    except Exception as e:
        logging.error("Failed to insert match lookup in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def insert_odds_bulk(odds_to_store):
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.executemany(
            """
            INSERT IGNORE INTO odds (oddsapiMatchId, league, homeTeamName, awayTeamName, commence_time, lastUpdated)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                commence_time=VALUES(commence_time),
                lastUpdated=VALUES(lastUpdated);
            """,
            odds_to_store,
        )

        conn.commit()
        return True

    except Exception as e:
        logging.error("Failed to insert odds in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def upload_prediction_edge_results(prediction_results):
    try:
        conn = get_db_conn()
        c = conn.cursor()

        current_utc_time = dt.datetime.now(timezone.utc)
        current_utc_time = current_utc_time.strftime("%Y-%m-%d %H:%M:%S")

        """
        {
            'edge_scores': edge_scores,
            'correct_winner_results': correct_winner_results,
            'uids': prediction_rewards_uids,
            'hotkeys': prediction_results_hotkeys,
            'sports': prediction_sports,
            'leagues': prediction_leagues
        }
        """

        # Prepare the data for executemany
        data_to_insert = list(
            zip(
                prediction_results["hotkeys"],
                prediction_results["uids"],
                prediction_results["leagues"],
                prediction_results["sports"],
                [1] * len(prediction_results["uids"]),
                prediction_results["correct_winner_results"],
                prediction_results["edge_scores"],
            )
        )

        prediction_edge_scores_table_name = "MatchPredictionEdgeResults"
        if not IS_PROD:
            prediction_edge_scores_table_name += "_test"
        c.executemany(
            f"""
            INSERT INTO {prediction_edge_scores_table_name} (
                miner_hotkey,
                miner_uid,
                league,
                sport,
                total_predictions,
                winner_predictions,
                avg_edge,
                last_updated
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, NOW()
            ) ON DUPLICATE KEY UPDATE
                total_predictions = total_predictions + VALUES(total_predictions),
                winner_predictions = winner_predictions + VALUES(winner_predictions),
                avg_edge = ((avg_edge * (total_predictions - VALUES(total_predictions))) + (VALUES(avg_edge) * VALUES(total_predictions))) / total_predictions,
                last_updated = NOW();
            """,
            data_to_insert,
        )

        conn.commit()
        logging.info("Prediction edge results data inserted or updated in database")
        return True

    except Exception as e:
        logging.error("Failed to insert prediction edge results data in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()


def upload_scored_predictions(predictions, vali_hotkey):
    try:
        conn = get_db_conn()
        c = conn.cursor()

        current_utc_time = dt.datetime.now(timezone.utc)
        current_utc_time = current_utc_time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"Content of predictions: {predictions}")

        predictions = predictions.get('predictions', [])

        # Ensure predictions is a list
        if not isinstance(predictions, list):
            raise ValueError("predictions must be a list of dictionaries")

        # Prepare the data for executemany
        data_to_insert = [
            (
                int(prediction.get("minerId")),
                prediction.get("hotkey"),
                vali_hotkey,
                prediction.get("predictionDate"),
                prediction.get("matchId"),
                prediction.get("matchDate"),
                int(prediction.get("sport")),
                prediction.get("league"),
                1,
                current_utc_time,
                prediction.get("homeTeamName"),
                prediction.get("awayTeamName"),
                prediction.get("homeTeamScore"),
                prediction.get("awayTeamScore"),
                prediction.get("probabilityChoice"),
                float(prediction.get("probability")),
                float(prediction.get("closingEdge")) if prediction.get("closingEdge") is not None else None,
            )
            for prediction in predictions
        ]

        prediction_scores_table_name = "MatchPredictionsScored"
        if not IS_PROD:
            prediction_scores_table_name += "_test"
        c.executemany(
            f"""
            INSERT IGNORE INTO {prediction_scores_table_name} (
                miner_id,
                miner_hotkey,
                vali_hotkey,
                predictionDate,
                matchId,
                matchDate,
                sport,
                league,
                isScored,
                scoredDate,
                homeTeamName,
                awayTeamName,
                homeTeamScore,
                awayTeamScore,
                probabilityChoice,
                probability,
                closingEdge
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            );
            """,
            data_to_insert,
        )

        conn.commit()
        logging.info("Scored predictions inserted in database")
        return True

    except Exception as e:
        logging.error("Failed to insert scored predictions in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()


def update_miner_reg_statuses(active_uids, active_hotkeys):
    try:
        conn = get_db_conn()
        c = conn.cursor()

        miners_table_name = "Miners"
        if not IS_PROD:
            miners_table_name += "_test"

        # mark all as unregistered first as we'll update only the active ones next
        c.execute(
            f"""
            UPDATE {miners_table_name}
            SET miner_is_registered = 0
            """,
        )
        conn.commit()

        # loop through zipped uids and hotkeys and update the miner_is_registered status
        for uid, hotkey in zip(active_uids, active_hotkeys):
            c.execute(
                f"""
                UPDATE {miners_table_name}
                SET miner_is_registered = 1, miner_uid = %s
                WHERE miner_hotkey = %s
                """,
                (uid, hotkey),
            )
            conn.commit()
        
        logging.info("Miner registration statuses updated in database")
        return True

    except Exception as e:
        logging.error("Failed to update miner registration statuses in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()


def insert_or_update_miner_coldkeys_and_ages(data_to_update):
    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        miners_table_name = "Miners"
        if not IS_PROD:
            miners_table_name += "_test"

        # mark all as unregistered first as we'll update only the active ones next
        cursor.execute(
            f"""
            UPDATE {miners_table_name}
            SET miner_is_registered = 0
            """,
        )
        conn.commit()

        cursor.executemany(f"""
            INSERT INTO {miners_table_name} (miner_hotkey, miner_coldkey, miner_uid, miner_age, miner_is_registered, last_updated) 
            VALUES (%s, %s, %s, %s, 1, NOW())
            ON DUPLICATE KEY UPDATE
                miner_coldkey=VALUES(miner_coldkey), 
                miner_age=VALUES(miner_age), 
                miner_is_registered=VALUES(miner_is_registered)
        """, data_to_update)

        # Commit the changes
        conn.commit()

    except Exception as e:
        logging.error("Failed to update miner coldkeys and ages in MySQL database", exc_info=True)
    finally:
        cursor.close()
        conn.close()


def get_prediction_stats_by_league(league=None, miner_hotkey=None, cutoff = None):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        prediction_scores_table_name = "MatchPredictionsScored"
        params = []
        miners_table_name = "Miners"
        if not IS_PROD:
            # prediction_scores_table_name += "_test"
            miners_table_name += "_test"

        query = f"""
            SELECT
                mps.miner_id,
                mps.miner_hotkey,
                JSON_ARRAYAGG(
                    JSON_OBJECT(
                        'vali_hotkey', mps.vali_hotkey,
                        'matchId', mps.matchId,
                        'matchDate', mps.matchDate,
                        'sport', mps.sport,
                        'league', mps.league,
                        'homeTeamName', mps.homeTeamName,
                        'awayTeamName', mps.awayTeamName,
                        'probabilityChoice', mps.probabilityChoice,
                        'probability', mps.probability,
                        'predictionDate', mps.predictionDate,
                        'closingEdge', mps.closingEdge,
                        'isScored', mps.isScored,
                        'scoredDate', mps.scoredDate,
                        'lastUpdated', mps.lastUpdated,
                        'homeTeamScore', matches.homeTeamScore,
                        'awayTeamScore', matches.awayTeamScore,
                        'closing_homeTeamOdds', closing_odds.homeTeamOdds,
                        'closing_awayTeamOdds', closing_odds.awayTeamOdds,
                        'closing_drawOdds', closing_odds.drawOdds
                    )
                ) AS data
            FROM {prediction_scores_table_name} mps
            LEFT JOIN {miners_table_name} m ON mps.miner_hotkey = m.miner_hotkey
            LEFT JOIN matches ON matches.matchId = mps.matchId
            LEFT JOIN matches_lookup ml ON (ml.matchId = mps.matchId)
            LEFT JOIN (
                SELECT 
                    mo.oddsapiMatchId,
                    mo.homeTeamOdds,
                    mo.awayTeamOdds,
                    mo.drawOdds,
                    ROW_NUMBER() OVER (PARTITION BY mo.oddsapiMatchId ORDER BY mo.lastUpdated DESC) as rn
                FROM 
                    match_odds mo
            ) closing_odds ON closing_odds.oddsapiMatchId = ml.oddsapiMatchId AND closing_odds.rn = 1
            WHERE m.miner_is_registered = 1
        """

        if cutoff:
            # Calculate the current timestamp
            current_timestamp = int(time.time())
            # Calculate cutoff date timestamp
            match_cutoff_timestamp = current_timestamp - (
                cutoff * 24 * 3600
            )
            # Convert timestamps to strings in 'YYYY-MM-DD HH:MM:SS' format
            match_cutoff_str = dt.datetime.utcfromtimestamp(
                match_cutoff_timestamp
            ).strftime("%Y-%m-%d %H:%M:%S")
            query += " AND mps.scoredDate > %s"
            params = [match_cutoff_str]

        if league:
            query += " AND league = %s"
            params.append(league)

        if miner_hotkey:
            query += " AND mps.miner_hotkey = %s"
            params.append(miner_hotkey)
        
        query += """ GROUP BY 
        mps.miner_id, 
        mps.miner_hotkey;"""

        if params:
            c.execute(query, params)
        else:
            c.execute(query)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query league prediction stats from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()

def get_total_bets_by_league(league=None, miner_hotkey=None):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        prediction_scores_table_name = "MatchPredictionsScored"
        params = []
        miners_table_name = "Miners"
        if not IS_PROD:
            # prediction_scores_table_name += "_test"
            miners_table_name += "_test"

        query = f"""
            SELECT
                DATE(mps.scoredDate) AS scoreDate,
                COUNT(*) AS total_bets
            FROM {prediction_scores_table_name} mps
            LEFT JOIN {miners_table_name} m ON mps.miner_hotkey = m.miner_hotkey
            WHERE m.miner_is_registered = 1
        """
        if league:
            query += " AND league = %s"
            params = [league]

        if miner_hotkey:
            query += " AND mps.miner_hotkey = %s"
            params.append(miner_hotkey)
        query += """
            GROUP BY DATE(mps.scoredDate)
            ORDER BY scoreDate ASC;
        """
        if params:
            c.execute(query, params)
        else:
            c.execute(query)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query league prediction stats from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()


def get_prediction_stats_by_sport(sport, miner_hotkey=None, group_by_miner=False):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        prediction_scores_table_name = "MatchPredictionResults"
        if not IS_PROD:
            prediction_scores_table_name += "_test"

        query = f"""
            SELECT
                sport,
                AVG(avg_score) AS avg_score,
                SUM(total_predictions) AS total_predictions,
                SUM(winner_predictions) AS winner_predictions
        """

        if group_by_miner:
            query += ", miner_hotkey, miner_coldkey, miner_uid, miner_age"

        query += f"""
            FROM {prediction_scores_table_name}
            WHERE sport = %s
        """

        params = [sport]

        if miner_hotkey:
            query += " AND miner_hotkey = %s"
            params.append(miner_hotkey)
        else:
            query += " AND miner_is_registered = 1"

        if group_by_miner:
            query += " GROUP BY sport, miner_hotkey, miner_coldkey, miner_uid, miner_age"
        else:
            query += " GROUP BY sport"

        c.execute(query, params)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query sport prediction stats from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()


def get_prediction_stats_total(miner_hotkey=None, group_by_miner=False):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        prediction_scores_table_name = "MatchPredictionResults"
        if not IS_PROD:
            prediction_scores_table_name += "_test"

        query = f"""
            SELECT
                AVG(avg_score) AS avg_score,
                SUM(total_predictions) AS total_predictions,
                SUM(winner_predictions) AS winner_predictions
        """

        if group_by_miner:
            query += ", miner_hotkey, miner_coldkey, miner_age"

        query += f"""
            FROM {prediction_scores_table_name}
            WHERE 1=1
        """

        params = []

        if miner_hotkey:
            query += " AND miner_hotkey = %s"
            params.append(miner_hotkey)
        else:
            query += " AND miner_is_registered = 1"

        if group_by_miner:
            query += " GROUP BY miner_hotkey, miner_coldkey, miner_uid, miner_age"

        c.execute(query, params)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query total prediction stats from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()


def get_prediction_stat_snapshots(sport=None, league=None, miner_hotkey=None):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        query = f"""
            SELECT *
            FROM MPRSnapshots
            WHERE 1=1
        """

        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)

        if league:
            query += " AND league = %s"
            params.append(league)

        if miner_hotkey:
            query += " AND miner_hotkey = %s"
            params.append(miner_hotkey)
        else:
            query += " AND miner_is_registered = 1"

        query += " ORDER BY snapshot_date ASC"
        
        c.execute(query, params)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query match prediction snapshots from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()


def get_prediction_stat_snapshots(sport=None, league=None, miner_hotkey=None):
    try:
        conn = get_db_conn()
        c = conn.cursor(dictionary=True)

        query = f"""
            SELECT *
            FROM MPRSnapshots
            WHERE 1=1
        """

        params = []
        if sport:
            query += " AND sport = %s"
            params.append(sport)

        if league:
            query += " AND league = %s"
            params.append(league)

        if miner_hotkey:
            query += " AND miner_hotkey = %s"
            params.append(miner_hotkey)
        else:
            query += " AND miner_is_registered = 1"

        query += " ORDER BY snapshot_date ASC"
        
        c.execute(query, params)
        return c.fetchall()

    except Exception as e:
        logging.error(
            "Failed to query match prediction snapshots from MySQL database", exc_info=True
        )
        return False
    finally:
        c.close()
        conn.close()


def upsert_app_match_prediction(prediction, vali_hotkey):
    try:
        conn = get_db_conn()
        c = conn.cursor()

        current_utc_time = dt.datetime.now(timezone.utc)
        current_utc_time = current_utc_time.strftime("%Y-%m-%d %H:%M:%S")

        c.execute(
            """
            INSERT INTO AppMatchPredictions (app_request_id, matchId, matchDate, sport, league, homeTeamName, awayTeamName, homeTeamScore, awayTeamScore, isComplete, lastUpdated, miner_hotkey, vali_hotkey) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                matchId=VALUES(matchId), 
                matchDate=VALUES(matchDate), 
                sport=VALUES(sport),
                league=VALUES(league),
                homeTeamName=VALUES(homeTeamName), 
                awayTeamName=VALUES(awayTeamName),
                homeTeamScore=VALUES(homeTeamScore), 
                awayTeamScore=VALUES(awayTeamScore),
                isComplete=VALUES(isComplete), 
                lastUpdated=VALUES(lastUpdated),
                miner_hotkey=VALUES(miner_hotkey),
                vali_hotkey=VALUES(vali_hotkey)
            """,
            (
                prediction["app_request_id"],
                prediction["matchId"],
                prediction["matchDate"],
                prediction["sport"],
                prediction["league"],
                prediction["homeTeamName"],
                prediction["awayTeamName"],
                prediction.get("homeTeamScore"),  # These can be None, hence using get
                prediction.get("awayTeamScore"),
                prediction.get("isComplete", 0),  # Default to 0 if not provided
                current_utc_time,
                prediction.get("miner_hotkey"),  # This can be None
                vali_hotkey,  # This can be None
            ),
        )

        conn.commit()
        logging.info("Data inserted or updated in database")
        return True

    except Exception as e:
        logging.error("Failed to insert app match prediction in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()

def update_app_match_predictions(predictions):
    try:
        conn = get_db_conn()
        c = conn.cursor()

        current_utc_time = dt.datetime.now(timezone.utc)
        current_utc_time = current_utc_time.strftime("%Y-%m-%d %H:%M:%S")

        predictions_to_update = []
        predictions_with_issues = []
        for prediction in predictions:
            if "minerHasIssue" in prediction and (prediction["minerHasIssue"] == 1 or prediction["minerHasIssue"] == True):
                predictions_with_issues.append(
                    (
                        current_utc_time,
                        1,
                        prediction["minerIssueMessage"],
                        prediction["app_request_id"],
                    )
                )
                continue

            if "homeTeamScore" not in prediction or "awayTeamScore" not in prediction:
                logging.error("Missing homeTeamScore or awayTeamScore for prediction in update_app_match_predictions")
                continue
            
            predictions_to_update.append(
                (
                    prediction["homeTeamScore"],
                    prediction["awayTeamScore"],
                    1,
                    current_utc_time,
                    prediction["app_request_id"],
                )
            )

        if predictions_to_update:
            c.executemany(
                """
                UPDATE AppMatchPredictions
                SET homeTeamScore = %s, awayTeamScore = %s, isComplete = %s, lastUpdated = %s
                WHERE app_request_id = %s
                """,
                predictions_to_update
            )
        if predictions_with_issues:
            c.executemany(
                """
                UPDATE AppMatchPredictions
                SET valiLastUpdated = %s, minerHasIssue = %s, minerIssueMessage = %s
                WHERE app_request_id = %s
                """,
                predictions_with_issues
            )

        conn.commit()
        logging.info("Data inserted or updated in database")
        return True

    except Exception as e:
        logging.error("Failed to insert match in MySQL database", exc_info=True)
        return False
    finally:
        c.close()
        conn.close()


def get_app_match_predictions_by_ids(prediction_ids, batch_size=-1):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        query = """SELECT m.*, apm.app_request_id, apm.isComplete AS predictionIsComplete, apm.homeTeamScore AS predictedHomeTeamScore, apm.awayTeamScore AS predictedAwayTeamScore, apm.lastUpdated AS predictionLastUpdated,
                       apm.miner_hotkey, apm.vali_hotkey, apm.valiLastUpdated, apm.minerHasIssue, apm.minerIssueMessage
            FROM AppMatchPredictions apm 
            LEFT JOIN matches m ON (m.matchId = apm.matchId) 
            WHERE 1=1 """
        
        params = []
        if prediction_ids is not None and len(prediction_ids) > 0:
            placeholders = ', '.join(['%s'] * len(prediction_ids))
            query += f" AND app_request_id IN ({placeholders})"
            params.extend(prediction_ids)
        if batch_size > 0:
            query += " LIMIT %s"
            params.append(batch_size)
            
        cursor.execute(query, params)
        requests_list = cursor.fetchall()

        return requests_list

    except Exception as e:
        logging.error(
            "Failed to retrieve app match predictions by ids from the MySQL database",
            exc_info=True,
        )
        return False
    finally:
        cursor.close()
        conn.close()


def get_app_match_predictions(vali_hotkey=None, batch_size=-1):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        query = "SELECT * FROM AppMatchPredictions WHERE isComplete = 0"
        
        params = []
        if vali_hotkey is not None:
            query += " AND vali_hotkey = %s"
            params.append(vali_hotkey)
        if batch_size > 0:
            query += " LIMIT %s"
            params.append(batch_size)
            
        cursor.execute(query, params)
        match_list = cursor.fetchall()

        # if results, we need to update the valiLastUpdated field
        if match_list and vali_hotkey is not None:
            cursor.execute(
                "UPDATE AppMatchPredictions SET valiLastUpdated = NOW() WHERE app_request_id IN (%s)"
                % ",".join(["%s"] * len(match_list)),
                [match["app_request_id"] for match in match_list],
            )
            conn.commit()

        return match_list

    except Exception as e:
        logging.error(
            "Failed to retrieve app match predictions from the MySQL database",
            exc_info=True,
        )
        return False
    finally:
        cursor.close()
        conn.close()


def get_app_match_predictions_unfulfilled(unfulfilled_threshold=5):
    try:
        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            f"SELECT * FROM AppMatchPredictions WHERE isComplete = 0 AND valiLastUpdated IS NULL AND lastUpdated < NOW() - INTERVAL {unfulfilled_threshold} MINUTE"
        )
        match_list = cursor.fetchall()

        return match_list

    except Exception as e:
        logging.error(
            "Failed to retrieve unfulfilled app match predictions from the MySQL database",
            exc_info=True,
        )
        return False
    finally:
        cursor.close()
        conn.close()


def get_prediction_by_id(app_id):
    try:
        # Log the received app_id
        logging.info(f"Fetching prediction for app_request_id: {app_id}")

        # Ensure app_id is a string
        if not isinstance(app_id, str):
            app_id = str(app_id)

        conn = get_db_conn()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT m.*, apm.app_request_id, apm.isComplete AS predictionIsComplete, apm.homeTeamScore AS predictedHomeTeamScore, apm.awayTeamScore AS predictedAwayTeamScore, apm.lastUpdated AS predictionLastUpdated,
                       apm.miner_hotkey, apm.vali_hotkey, apm.valiLastUpdated, apm.minerHasIssue, apm.minerIssueMessage
            FROM AppMatchPredictions apm 
            LEFT JOIN matches m ON (m.matchId = apm.matchId) 
            WHERE app_request_id = %s
        """, (app_id,)
        )
        prediction = cursor.fetchone()

        return prediction
    except Exception as e:
        logging.error(
            "Failed to retrieve prediction from the MySQL database", exc_info=True
        )
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def create_tables():
    c = None
    conn = None
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS matches (
            matchId VARCHAR(50) PRIMARY KEY,
            matchDate TIMESTAMP NOT NULL,
            sport INTEGER NOT NULL,
            homeTeamName VARCHAR(30) NOT NULL,
            awayTeamName VARCHAR(30) NOT NULL,
            homeTeamScore INTEGER,
            awayTeamScore INTEGER,
            matchLeague VARCHAR(50),
            isComplete BOOLEAN DEFAULT FALSE,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS matches_lookup (
            matchId VARCHAR(50) PRIMARY KEY,
            sportsdbMatchId VARCHAR(50) DEFAULT NULL,
            oddsapiMatchId VARCHAR(50) DEFAULT NULL
        )"""
        )

        c.execute(
            """
        CREATE TABLE IF NOT EXISTS match_odds (
            id VARCHAR(50) PRIMARY KEY,
            oddsapiMatchId VARCHAR(50) DEFAULT NULL,
            homeTeamOdds FLOAT,
            awayTeamOdds FLOAT,
            drawOdds FLOAT,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
        )

        c.execute(
            """
        CREATE TABLE IF NOT EXISTS odds (
            oddsapiMatchId VARCHAR(50) PRIMARY KEY,
            league VARCHAR(30) NOT NULL,
            homeTeamName VARCHAR(30) NOT NULL,
            awayTeamName VARCHAR(30) NOT NULL,
            commence_time TIMESTAMP NOT NULL,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
       )"""
       )

        c.execute(
            """
        CREATE TABLE IF NOT EXISTS MatchPredictionsScored (
            miner_id INTEGER NOT NULL,
            miner_hotkey VARCHAR(64) NOT NULL,
            vali_hotkey VARCHAR(64) NOT NULL,
            predictionDate TIMESTAMP NOT NULL,
            matchId VARCHAR(50) NOT NULL,
            matchDate TIMESTAMP NOT NULL,
            sport INTEGER NOT NULL,
            league VARCHAR(50) NOT NULL,
            homeTeamName VARCHAR(30) NOT NULL,
            awayTeamName VARCHAR(30) NOT NULL,
            homeTeamScore INTEGER,
            awayTeamScore INTEGER,
            probabilityChoice VARCHAR(10) NOT NULL,
            probability FLOAT NOT NULL,
            closingEdge FLOAT NOT NULL,
            isScored BOOLEAN DEFAULT FALSE,
            scoredDate TIMESTAMP DEFAULT NULL,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (miner_hotkey, vali_hotkey, matchId)
        )"""
        )

        c.execute(
            """
        CREATE TABLE IF NOT EXISTS MatchPredictionsScored_test (
            miner_id INTEGER NOT NULL,
            miner_hotkey VARCHAR(64) NOT NULL,
            vali_hotkey VARCHAR(64) NOT NULL,
            predictionDate TIMESTAMP NOT NULL,
            matchId VARCHAR(50) NOT NULL,
            matchDate TIMESTAMP NOT NULL,
            sport INTEGER NOT NULL,
            league VARCHAR(50) NOT NULL,
            homeTeamName VARCHAR(30) NOT NULL,
            awayTeamName VARCHAR(30) NOT NULL,
            homeTeamScore INTEGER,
            awayTeamScore INTEGER,
            probabilityChoice VARCHAR(10) NOT NULL,
            probability FLOAT NOT NULL,
            closingEdge FLOAT NOT NULL,
            isScored BOOLEAN DEFAULT FALSE,
            scoredDate TIMESTAMP DEFAULT NULL,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (miner_hotkey, vali_hotkey, matchId)
        )"""
        )

        c.execute(
            """
        CREATE TABLE IF NOT EXISTS MatchPredictionResults (
            id INT AUTO_INCREMENT PRIMARY KEY,
            miner_hotkey VARCHAR(64) NOT NULL,
            miner_coldkey VARCHAR(64) NOT NULL,
            miner_uid INTEGER NOT NULL,
            miner_is_registered TINYINT(1) DEFAULT 1,
            miner_age INTEGER NOT NULL DEFAULT 0,
            league VARCHAR(50) NOT NULL,
            sport INTEGER NOT NULL,
            total_predictions INTEGER NOT NULL,
            winner_predictions INTEGER NOT NULL,
            avg_score FLOAT NOT NULL,
            last_updated TIMESTAMP NOT NULL,
            UNIQUE (miner_hotkey, miner_uid, league)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS MatchPredictionResults_test (
            id INT AUTO_INCREMENT PRIMARY KEY,
            miner_hotkey VARCHAR(64) NOT NULL,
            miner_coldkey VARCHAR(64) NOT NULL,
            miner_uid INTEGER NOT NULL,
            miner_is_registered TINYINT(1) DEFAULT 1,
            miner_age INTEGER NOT NULL DEFAULT 0,
            league VARCHAR(50) NOT NULL,
            sport INTEGER NOT NULL,
            total_predictions INTEGER NOT NULL,
            winner_predictions INTEGER NOT NULL,
            avg_score FLOAT NOT NULL,
            last_updated TIMESTAMP NOT NULL,
            UNIQUE (miner_hotkey, miner_uid, league)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS Miners (
            id INT AUTO_INCREMENT PRIMARY KEY,
            miner_hotkey VARCHAR(64) NOT NULL,
            miner_coldkey VARCHAR(64) NOT NULL,
            miner_uid INTEGER NOT NULL,
            miner_is_registered TINYINT(1) DEFAULT 1,
            miner_age INTEGER NOT NULL DEFAULT 0,
            last_updated TIMESTAMP NOT NULL,
            UNIQUE (miner_hotkey, miner_uid)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS Miners_test (
            id INT AUTO_INCREMENT PRIMARY KEY,
            miner_hotkey VARCHAR(64) NOT NULL,
            miner_coldkey VARCHAR(64) NOT NULL,
            miner_uid INTEGER NOT NULL,
            miner_is_registered TINYINT(1) DEFAULT 1,
            miner_age INTEGER NOT NULL DEFAULT 0,
            last_updated TIMESTAMP NOT NULL,
            UNIQUE (miner_hotkey, miner_uid)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS MPRSnapshots (
            snapshot_id INT AUTO_INCREMENT PRIMARY KEY,
            snapshot_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            id INT,
            miner_hotkey VARCHAR(64),
            miner_coldkey VARCHAR(64),
            miner_uid INTEGER,
            miner_is_registered TINYINT(1),
            miner_age INTEGER NOT NULL DEFAULT 0,
            league VARCHAR(50),
            sport INTEGER,
            total_predictions INTEGER,
            winner_predictions INTEGER,
            avg_score FLOAT,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
        )
        conn.commit()
    except Exception as e:
        logging.error("Failed to create matches table in MySQL database", exc_info=True)
    finally:
        if c is not None:
            c.close()
        if conn is not None:
            conn.close()


def create_app_tables():
    c = None
    conn = None
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS AppMatchPredictions (
            app_request_id VARCHAR(50) PRIMARY KEY,
            matchId VARCHAR(50) NOT NULL,
            matchDate TIMESTAMP NOT NULL,
            sport INTEGER NOT NULL,
            league VARCHAR(50) NOT NULL,
            homeTeamName VARCHAR(30) NOT NULL,
            awayTeamName VARCHAR(30) NOT NULL,
            homeTeamScore INTEGER,
            awayTeamScore INTEGER,
            isComplete BOOLEAN DEFAULT FALSE,
            lastUpdated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            miner_hotkey VARCHAR(64) NULL,
            vali_hotkey VARCHAR(64) NULL,
            valiLastUpdated TIMESTAMP NULL,
            minerHasIssue BOOLEAN DEFAULT FALSE,
            minerIssueMessage VARCHAR(255) NULL
        )"""
        )
        conn.commit()
    except Exception as e:
        logging.error("Failed to create matches table in MySQL database", exc_info=True)
    finally:
        if c is not None:
            c.close()
        if conn is not None:
            conn.close()


def get_db_conn():
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
        return conn
    except mysql.connector.Error as e:
        logging.error(f"Failed to connect to MySQL database: {e}")
        return None  # Explicitly return None if there is an error


create_tables()
create_app_tables()
