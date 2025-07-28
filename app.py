from openai import OpenAI
import boto3
from boto3.dynamodb.conditions import Key
from datetime import datetime
import json
import requests
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("MatchingResults")

def get_parameter(name, with_decryption=False):
    return ssm.get_parameter(Name=name, WithDecryption=with_decryption)["Parameter"]["Value"]

OPEN_AI_API_KEY = get_parameter("/jobsourcing/env/dev/lmm/openai/key", with_decryption=True)
OPEN_AI_BASE_URL = "https://api.openai.com/v1"
client = OpenAI(api_key=OPEN_AI_API_KEY , base_url=OPEN_AI_BASE_URL)
CLIENT_SECRET_KEYCLOAK=get_parameter("/llama/client_secret_keycloak", with_decryption=True)
gateway_url = get_parameter("/jobsourcing/env/dev/gateway/url")


def fetch_offer_dto(gateway_url, headers ,offer_id):
    url = f"{gateway_url}/JOB-OFFER-SERVICE/api/v1/job-offers/{offer_id}/matching"
    logger.info(f"Fetching offer from: {url}")
    response = requests.get(url,headers=headers, timeout=30)
    response.raise_for_status()
    return response

def fetch_profile_dto(gateway_url,headers ,candidate_id ,profile_id):
    url = f"{gateway_url}/CANDIDATE-SERVICE/api/v1/candidates/{candidate_id}/profiles/{profile_id}/matching"
    logger.info(f"Fetching profile from: {url}")
    response = requests.get(url,headers=headers, timeout=30)
    response.raise_for_status()
    return response

def get_access_token():
    token_url = "https://job-sourcing.com/realms/jobsourcingrealm/protocol/openid-connect/token"
    client_id = "lamdaParsingAiClient"
    client_secret = CLIENT_SECRET_KEYCLOAK

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    }

    response = requests.post(token_url, data=data)
    response.raise_for_status()

    return response.json()["access_token"]



# Declare weight variables as constants
SKILLS_WEIGHT = 0.40
EXPERIENCE_WEIGHT = 0.30
EDUCATION_WEIGHT = 0.10
LANGUAGE_WEIGHT = 0.10
LOCATION_WEIGHT = 0.05
TITLE_WEIGHT=0.05




def update_openai_result_in_dynamodb(
    offer_id, profile_id,
    candidate_id,
    openai_result: dict
):
    now = datetime.utcnow().isoformat()

    final_score_raw = openai_result.get("final_score", 0)
    final_score = Decimal(0)
    try:
        final_score = Decimal(str(final_score_raw)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        final_score = Decimal(0)
    
    openai_result.pop("final_score", None)  # Safely remove if it exists

    response = table.update_item(
        Key={
            'offerId': offer_id,
            'profileId': profile_id
        },
        UpdateExpression="""
            SET 
                candidateId = :cid,
                openAiMatchDetails = :openai,
                updatedAt = :upd,
                totalMatchScoreAdvanced = :ts,
                createdAt = if_not_exists(createdAt, :cre)
        """,
        ExpressionAttributeValues={
            ':cid': candidate_id,
            ':ts' : final_score ,
            ':openai': openai_result,
            ':upd': now,
            ':cre': now
        },
        ReturnValues="UPDATED_NEW"
    )

    return response


def lambda_handler(event, context):
    try:
        # Expecting a single record in batch (batchSize = 1)
        record = event['Records'][0]
        body = json.loads(record['body'])

        offer_id = body.get("offerId")
        profile_id = body.get("profileId")
        candidate_id = body.get("candidateId")                 # JSON object
        # Validate presence of all required fields
        missing_fields = []
        if not offer_id:
            missing_fields.append("offerId")
        if not profile_id:
            missing_fields.append("profileId")
        if not candidate_id:
            missing_fields.append("candidateId")

        if missing_fields:
            raise ValueError(f"Missing required fields in message: {', '.join(missing_fields)}")

        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        offer_res = fetch_offer_dto(gateway_url, headers ,offer_id)
        logger.info(f"offerFetch: {offer_res}")
        offer_json= offer_res.json()
        profile_res = fetch_profile_dto(gateway_url,headers ,candidate_id ,profile_id)
        logger.info(f"profileFetch: {profile_res}")
        profile_json=profile_res.json()

        if not offer_json or not profile_json:
            logger.error("Missing 'offerDto' or 'profileDto' in the payload")
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required fields"})
            }


        prompt = f"""
            You are a recruitment AI assistant. Your task is to analyze how well a candidate matches a job offer.
            You will compare both the structured profile data and the free-text resume against the offer.
            Important Guidelines:

            - Give special importance to the requirement level ("REQUIRED", "PREFERRED", "OPTIONAL") specified in the job offer for each skill, language, experience, and education requirement.
            - Each dimension should reflect how well the candidate meets or exceeds the required and preferred criteria.
            - The score for each field should be an integer between 0 and 100.
            - Use the following weights to calculate final_score:

                - skills_match ({SKILLS_WEIGHT*100}%)
                - experience_match ({EXPERIENCE_WEIGHT*100}%)
                - education_match ({EDUCATION_WEIGHT*100}%)
                - language_match ({LANGUAGE_WEIGHT*100}%)
                - profile_title_match ({TITLE_WEIGHT*100}%)
                - location_match ({LOCATION_WEIGHT*100}%)

            Here is the structured candidate profile:
            {profile_json}

            Here is the job offer:
            {offer_json}

            You are evaluating a candidate profile against a job offer. Based on the provided structured data and analysis logic, please return ONLY a JSON object with the following fields:

            {{
            "skills_match": {{
                "score": <0-100>,
                "matched": ["<list of matched skills>"],
                "missing": ["<list of missing or weak skills>"]
            }},
            "experience_match": {{
                "score": <0-100>,
                "matched": ["<list of matched experience elements>"],
                "missing": ["<missing or insufficient experience>"]
            }},
            "education_match": {{
                "score": <0-100>,
                "matched": ["<list of matching degrees/fields>"],
                "missing": ["<missing or insufficient education>"]
            }},
            "language_match": {{
                "score": <0-100>,
                "matched": ["<languages matched>"],
                "missing": ["<languages required but missing>"]
            }},
            "location_match": {{
                "score": <0-100>,
                "matched": ["<location match>"],
                "missing": ["<location issues if any>"]
            }},
            "profile_title_match": {{
                "score": <0-100>,
                "matched": ["<profile title matched>"],
                "missing": ["<what title was expected but missing>"]
            }},
            "final_score": <0-100>,
            "reasoning": "<your reasoning based on all elements>",
            "red_flags": {{
                "<Skill>": "<reason why it's a concern or not matched>",
                "<Skill>": "<another concern or issue>",
                ...
            }},
            "estimated_seniority": "<Junior | Mid-level | Senior | Lead>",
            "growth_potential": "<comment on the potential for upskilling or growth>",
            "recommended_training": ["<list of suggested skills, tools, or certifications to improve match>"]
            }}

            Do not add any explanation, header, or surrounding text. Just return the JSON object exactly as specified.
            """

        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are an expert recruitment assistant."},
                {"role": "user", "content": prompt}
            ],
            stream=False
        )

        logger.info(f"Before OpenAI Response: {response.choices[0].message.content}")

        openai_result=None
        # Convert string to dict
        try:
            openai_result = json.loads(response.choices[0].message.content)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse OpenAI response as JSON", exc_info=e)
            raise

        logger.info(f"OpenAI Response: {openai_result}")


        # Save to DynamoDB
        response = update_openai_result_in_dynamodb(
            offer_id=offer_id,
            profile_id=profile_id,
            candidate_id=candidate_id,
            openai_result=openai_result
        )

        return {
            "statusCode": 200
        }

    except Exception as e:
        logger.error(f"Error while processing: {e}")
        return {
            "statusCode": 500,
            "body": f"Error: {str(e)}"
        }