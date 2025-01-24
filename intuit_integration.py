import aiohttp
import asyncio
import json
import os
from typing import Any, Dict, Optional
from fake_useragent import UserAgent

from submodule_integrations.models.integration import Integration
from submodule_integrations.utils.errors import (
    IntegrationAuthError,
    IntegrationAPIError,
)


class IntuitIntegration(Integration):
    def __init__(self, user_agent: str = UserAgent().random):
        super().__init__("intuit")
        self.user_agent = user_agent
        self.network_requester = None
        self.url = "https://protaxdata.api.intuit.com"

    async def initialize(self, authorization: str, cookie: str, network_requester=None):
        self.network_requester = network_requester
        self.headers = {
            "accept": "application/json",
            "authorization": authorization,
            "cookie": cookie,
        }
        with open('submodule_integrations/intuit/format.json', 'r') as file:
            self.DEFAULT_TEMPLATE = json.load(file)

    async def _make_request(self, method: str, url: str, **kwargs) -> str:
        """
        Helper method to make network requests.
        """
        if self.network_requester:
            response = await self.network_requester.request(
                method, url, process_response=self._handle_response, **kwargs
            )
            return response
        else:
            async with aiohttp.ClientSession() as session:
                if method == "PUT":
                    async with session.put(url, **kwargs) as response:
                        return await self._handle_response(response)
                else:
                    async with session.request(method, url, **kwargs) as response:
                        return await self._handle_response(response)

    async def _handle_response(self, response: aiohttp.ClientResponse) -> Any:
        """
        Handle the response from Intuit's API.
        """
        if response.status in [200, 201]:
            return await response.json()

        response_json = await response.json()

        if response.status == 401:
            error_message = response_json.get("message", "Authentication failed.")
            raise IntegrationAuthError(
                f"Appfolio: Authentication failed. (HTTP {response.status})",
                response.status,
                response.status
            )

        error_message = response_json.get("error", "Unknown error occurred.")
        raise IntegrationAPIError(
            self.integration_name,
            f"{error_message} (HTTP {response.status})",
            response.status,
            response.status,
        )

    async def get_client_info(self, return_year: Optional[int] = None) -> Dict[str, Any]:
        """
        Fetch clients from Intuit's API and optionally enrich with their tax return status for a specific year.

        Args:
            return_year: Optional year to fetch tax return status. If provided, adds return status to client info.

        Returns:
            Dictionary containing client information with optional return status.
        """
        # Fetch base client information
        endpoint = "/v2/clients"
        api_url = f"{self.url}{endpoint}"
        client_response = await self._make_request(method="GET", url=api_url, headers=self.headers)

        # If no return year provided, return just client info
        if return_year is None:
            return client_response

        # Fetch tax return status for the given year
        returns_endpoint = f"/v1/returns/filter/{return_year}"
        returns_api_url = f"{self.url}{returns_endpoint}"
        returns_response = await self._make_request(method="GET", url=returns_api_url, headers=self.headers)

        # Fetch status descriptions
        status_endpoint = "/v1/returnstatus"
        status_api_url = f"{self.url}{status_endpoint}"
        status_response = await self._make_request(method="GET", url=status_api_url, headers=self.headers)

        # Create status ID to description mapping
        status_map = {
            status['id']: status['description']
            for status in status_response.get('values', [])
        }

        # Create a mapping of client IDs to their return status
        client_status_map = {
            entry['id_client']: status_map.get(entry['id_status'], "Unknown Status")
            for entry in returns_response
        }

        # Add return status to each client's information
        for client in client_response:
            client['return_status'] = client_status_map.get(client.get('clientId'), "No Return Found")

        return client_response

    async def get_series_version(self, client_id: str, return_id: str) -> str:
        """
        Fetch the version information for a specific tax return.
        """
        endpoint = f"/v2/clients/{client_id}/returns/{return_id}"
        api_url = f"{self.url}{endpoint}"
        response = await self._make_request(method="GET", url=api_url, headers=self.headers)

        # Extract the s11 version from seriesVersion
        for version_info in response.get("seriesVersion", []):
            if version_info.get("series") == "s11":
                return version_info.get("version")

        # If no s11 version is found, raise an error
        raise IntegrationAPIError(
            self.integration_name,
            "No s11 version found in response",
            404,
            404
        )

    async def update_w2_data(self, client_id: str, return_id: str, payload: dict):
        """
        Update W2 data for a specific client and return using the PUT request.

        Args:
            client_id: The ID of the client.
            return_id: The ID of the tax return.
            data: The data to update in the W2 form.
        """

        # Initialize the data
        data = self.DEFAULT_TEMPLATE.copy()
        data["returnId"] = return_id
        data["clientId"] = client_id

        # Get the s11 and s1 data
        endpoint = f"/v2/clients/{client_id}/returns/{return_id}"
        api_url = f"{self.url}{endpoint}"
        response = await self._make_request(method="GET", url=api_url, headers=self.headers)

        # Extract the s11 version from seriesVersion
        for version_info in response.get("seriesVersion", []):
            if version_info.get("series") == "s11":
                s11_data = version_info.get("version")
            if version_info.get("series") == "s1":
                s1_data = version_info.get("version")
        api_url = "https://inputviewcatalog.api.intuit.com/v2/input-views/24ind11/data"

        # If S1/11 data is provided, update it
        if s11_data:
            data["version"]["s11"] = s11_data
            data["version"]["s1"] = s1_data

            # Base employer info
        data["ind"]["detail"]["s11"]["p"][0]["c807"]["x"] = [{"desc": payload.ein}]
        data["ind"]["detail"]["s11"]["p"][0]["c805"]["x"] = [{"desc": payload.employer_state_id}]
        data["ind"]["detail"]["s11"]["p"][0]["c806"]["x"] = [{"desc": payload.name}]

        # State ID verification flag - now using empty array for false
        data["ind"]["detail"]["s11"]["p"][0]["c282"]["x"] = [{"amt": "1"}] if payload.address.state_id_verified else []

        # Foreign address flag
        data["ind"]["detail"]["s11"]["p"][0]["c88"]["x"] = [{"amt": "1"}] if payload.address.is_foreign else []

        # Address handling

        # Domestic address
        data["ind"]["detail"]["s11"]["p"][0]["c811"]["x"] = [{"desc": payload.address.street}]
        data["ind"]["detail"]["s11"]["p"][0]["c812"]["x"] = [{"desc": payload.address.city}]
        data["ind"]["detail"]["s11"]["p"][0]["c820"]["x"] = [{"desc": payload.address.state}]
        data["ind"]["detail"]["s11"]["p"][0]["c821"]["x"] = [{"desc": payload.address.zip}]

        # Foreign address
        data["ind"]["detail"]["s11"]["p"][0]["c841"]["x"] = [{"desc": payload.address.foreign_address.region}]
        data["ind"]["detail"]["s11"]["p"][0]["c842"]["x"] = [{"desc": payload.address.foreign_address.postal_code}]
        data["ind"]["detail"]["s11"]["p"][0]["c843"]["x"] = [{"desc": payload.address.foreign_address.country}]

        response = await self._make_request(
            method="PUT",
            url=api_url,
            headers=self.headers,
            json=data,
        )
        return response
