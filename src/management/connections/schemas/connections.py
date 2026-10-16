from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

from management.connections.connectors.base import (
    Aggregate,
    Column,
    FieldNames,
    Identifier,
    Source,
    TagNames,
    check_series_names,
)
from management.connections.connectors.bigquery import (
    LOCATION_PATTERN,
    PROJECT_ID_PATTERN,
    SERVICE_ACCOUNT_MAX_CHARS,
    parse_service_account,
)
from management.connections.connectors.google_drive import parse_folder_id
from management.connections.connectors.google_sheets import (
    parse_spreadsheet_id,
)
from management.connections.connectors.http_source import parse_server_url
from management.connections.connectors.network_guard import is_host
from management.connections.connectors.timeseries import LiveWindow

PREVIEW_CELL_CHARS = 500


# A live view is at most 96 points a series: this many rows is some twenty series.
LIVE_MAX_ROWS = 2000


# A NUL byte is not valid in a PostgreSQL parameter and would make the driver raise.
ConfigText = Annotated[str, StringConstraints(min_length=1, max_length=253, pattern=r"^[^\x00]+$")]


ConnectionName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=120, pattern=r"^[^\x00]+$"),
]


class _ServerConfig(BaseModel, extra="forbid"):
    host: ConfigText = Field(description="A DNS name or an IP address, without port or scheme.")
    database: ConfigText
    username: ConfigText
    ssl: Literal["disable", "require", "verify-full"] = Field(
        default="require",
        description=(
            "`require` encrypts the connection but does not check the server certificate. "
            "`verify-full` also checks it against public certificate authorities and the "
            "host name; servers with a private CA need `require`."
        ),
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        # Refused here, before anything is audited: text pasted into this field by mistake
        # (a full connection URL, say) can carry a password.
        value = value.strip()
        if not is_host(value):
            raise ValueError("host must be a DNS name or an IP address")
        return value


class PostgresConfig(_ServerConfig):
    port: int = Field(default=5432, ge=1, le=65535)


class MysqlConfig(_ServerConfig):
    port: int = Field(default=3306, ge=1, le=65535)


class PasswordSecret(BaseModel, extra="forbid"):
    password: Annotated[str, StringConstraints(max_length=1024, pattern=r"^[^\x00]*$")] = ""


class CreatePostgresConnection(BaseModel, extra="forbid"):
    name: ConnectionName
    kind: Literal["postgres"]
    config: PostgresConfig
    secret: PasswordSecret


class CreateMysqlConnection(BaseModel, extra="forbid"):
    """MySQL or MariaDB. The database is the only schema the connection browses."""

    name: ConnectionName
    kind: Literal["mysql"]
    config: MysqlConfig
    secret: PasswordSecret


class BigQueryConfig(BaseModel, extra="forbid"):
    project_id: Annotated[str, StringConstraints(pattern=PROJECT_ID_PATTERN)] | None = Field(
        default=None,
        description=(
            "The project whose datasets are browsed and which is billed for queries. "
            "Left out, it is the project of the service account."
        ),
    )
    location: Annotated[str, StringConstraints(pattern=LOCATION_PATTERN)] | None = Field(
        default=None,
        description="Where queries run, such as `US` or `asia-southeast1`. Usually not needed.",
    )


class ServiceAccountSecret(BaseModel, extra="forbid"):
    service_account_json: Annotated[
        str, StringConstraints(min_length=1, max_length=SERVICE_ACCOUNT_MAX_CHARS)
    ] = Field(description="The content of the JSON key file of a Google service account.")

    @field_validator("service_account_json")
    @classmethod
    def validate_key_file(cls, value: str) -> str:
        parse_service_account(value)
        return value


class CreateBigQueryConnection(BaseModel, extra="forbid"):
    """BigQuery, as a service account. A dataset is what the other kinds call a schema."""

    name: ConnectionName
    kind: Literal["bigquery"]
    config: BigQueryConfig = Field(default_factory=BigQueryConfig)
    secret: ServiceAccountSecret

    @model_validator(mode="after")
    def default_project(self) -> "CreateBigQueryConnection":
        if self.config.project_id is None:
            account = parse_service_account(self.secret.service_account_json)
            self.config.project_id = account["project_id"]
        return self


class GoogleSheetsConfig(BaseModel, extra="forbid"):
    spreadsheet: Annotated[str, StringConstraints(min_length=1, max_length=2048)] = Field(
        description=(
            "The address of the spreadsheet as the browser shows it "
            "(`https://docs.google.com/spreadsheets/d/<id>/...`), or the ID alone."
        )
    )

    @field_validator("spreadsheet")
    @classmethod
    def validate_spreadsheet(cls, value: str) -> str:
        return parse_spreadsheet_id(value)


GRANT_ID = (
    "The `google_grant` the browser came back from Google with. It works once, for the user "
    "who started and in that project, within 10 minutes."
)
ACCOUNT_ID = (
    "The caller's own `google` connection in this project, whose access the new connection "
    "gets a copy of. Nothing is asked of Google again."
)


class CreateGoogleSheetsConnection(BaseModel, extra="forbid"):
    """One Google spreadsheet, read as the Google account that gave access.

    The spreadsheet is the only schema and each tab is a table whose first row names the
    columns. The saved `config` holds `spreadsheet_id`, `title`, `account_email` and
    `google_subject`.
    """

    name: ConnectionName
    kind: Literal["google_sheets"]
    config: GoogleSheetsConfig
    account_id: UUID = Field(description=ACCOUNT_ID)


class CreateGoogleConnection(BaseModel, extra="forbid"):
    """A member's Google account, kept for them alone to pick spreadsheets and folders with.

    It is not read itself and only its creator sees it. Each member has at most one in a
    project. The saved `config` holds `account_email` and `google_subject`.
    """

    name: ConnectionName | None = Field(
        default=None,
        description=(
            "Left out, `Google account`, with ` (2)`, ` (3)` and so on when the project "
            "already has a connection by that name."
        ),
    )
    kind: Literal["google"]
    grant_id: UUID = Field(description=GRANT_ID)


class GoogleDriveConfig(BaseModel, extra="forbid"):
    folder: Annotated[str, StringConstraints(min_length=1, max_length=2048)] = Field(
        description=(
            "The address of the folder as the browser shows it "
            "(`https://drive.google.com/drive/folders/<id>`), or the ID alone."
        )
    )

    @field_validator("folder")
    @classmethod
    def validate_folder(cls, value: str) -> str:
        return parse_folder_id(value)


class CreateGoogleDriveConnection(BaseModel, extra="forbid"):
    """One Google Drive folder, read as the Google account that gave access.

    Each CSV file, Google spreadsheet and Excel workbook (`.xlsx`) directly in the folder is
    a schema, named like the file, and each of its tabs is a table whose first row names the
    columns; a CSV file has one table, named like the file. Folders inside the folder are
    not read. The saved `config` holds `folder_id`, `folder_name`, `account_email` and
    `google_subject`.
    """

    name: ConnectionName
    kind: Literal["google_drive"]
    config: GoogleDriveConfig
    account_id: UUID = Field(description=ACCOUNT_ID)


class _HttpServerConfig(BaseModel, extra="forbid"):
    """A server spoken to over HTTP, by the address of its API."""

    url: Annotated[str, StringConstraints(min_length=1, max_length=2048)] = Field(
        description=(
            "`http(s)://host[:port][/base-path]`: the address in front of the path of the "
            "API (`/api/v1` for Prometheus, `/query` for InfluxDB). Without a user name, a "
            "password, a query or a fragment."
        )
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        # Refused here, before anything is audited: an address can carry a password.
        return parse_server_url(value).url

    # Saved with the address, never taken from the request: what is resolved, checked and
    # audited is the host the address itself names.
    @computed_field
    @property
    def host(self) -> str:
        return parse_server_url(self.url).host

    @computed_field
    @property
    def port(self) -> int:
        return parse_server_url(self.url).port


class PrometheusConfig(_HttpServerConfig):
    pass


# Printable ASCII without a space: nothing else can be sent in a header.
HeaderToken = Annotated[str, StringConstraints(max_length=4096, pattern=r"^[\x21-\x7e]*$")]


InfluxUsername = Annotated[
    str, StringConstraints(max_length=255, pattern=r"^[\x20-\x39\x3b-\x7e]*$")
]


InfluxPassword = Annotated[str, StringConstraints(max_length=4096, pattern=r"^[\x20-\x7e]*$")]


class PrometheusSecret(BaseModel, extra="forbid"):
    # A user name is printable ASCII too, and has no colon, which is what ends it in Basic
    # authentication.
    username: Annotated[
        str, StringConstraints(max_length=255, pattern=r"^[\x20-\x39\x3b-\x7e]*$")
    ] = Field(default="", description="With a user name, `token` is sent as its password.")
    token: HeaderToken = Field(
        default="", description="Alone, it is sent as a Bearer token. Empty for no credentials."
    )


class CreatePrometheusConnection(BaseModel, extra="forbid"):
    """Prometheus, or a server with the same HTTP API (VictoriaMetrics, Thanos, Mimir).

    `default` is the only schema, each metric is a table and each of its labels a column. It
    reads `timeseries` sources only, always with a `bucket`. The saved `config` holds `url`,
    `host` and `port`.
    """

    name: ConnectionName
    kind: Literal["prometheus"]
    config: PrometheusConfig
    secret: PrometheusSecret = Field(default_factory=PrometheusSecret)


class InfluxConfig(_HttpServerConfig):
    database: Annotated[
        str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
    ] = Field(description="The database (1.x), or the name of the bucket (2.x and 3).")


class InfluxSecret(BaseModel, extra="forbid"):
    token: HeaderToken = Field(
        default="",
        description="For InfluxDB 2.x and 3, sent as `Authorization: Token …`.",
    )
    username: InfluxUsername = Field(
        default="", description="For an authenticated InfluxDB 1.8 server; give with `password`."
    )
    password: InfluxPassword = Field(
        default="", description="For an authenticated InfluxDB 1.8 server; give with `username`."
    )

    @model_validator(mode="after")
    def _one_auth_method(self) -> "InfluxSecret":
        has_username = bool(self.username)
        has_password = bool(self.password)
        if has_username != has_password:
            raise ValueError("username and password must be given together")
        if self.token and has_username:
            raise ValueError("choose a token or username and password")
        return self


class CreateInfluxConnection(BaseModel, extra="forbid"):
    """InfluxDB 1.8, 2.x or 3 Core, read with InfluxQL.

    `default` is the only schema, each measurement of the database is a table, and each of
    its fields and tags a column. It reads `timeseries` sources only, always with at least
    one of `fields`. Use `secret.username` and `secret.password` together for authenticated
    InfluxDB 1.8, or `secret.token` for InfluxDB 2.x and 3. The saved `config` holds `url`,
    `database`, `host` and `port`.
    """

    name: ConnectionName
    kind: Literal["influxdb"]
    config: InfluxConfig
    secret: InfluxSecret = Field(default_factory=InfluxSecret)


CreateConnection = Annotated[
    CreatePostgresConnection
    | CreateMysqlConnection
    | CreateBigQueryConnection
    | CreateGoogleConnection
    | CreateGoogleSheetsConnection
    | CreateGoogleDriveConnection
    | CreatePrometheusConnection
    | CreateInfluxConnection,
    Field(discriminator="kind"),
]


class ReauthorizeConnection(BaseModel, extra="forbid"):
    grant_id: UUID


class RenameConnection(BaseModel, extra="forbid"):
    name: ConnectionName


class ConnectionItem(BaseModel):
    id: str
    project_id: str
    name: str
    kind: str
    config: dict[str, Any]
    last_tested_at: datetime | None
    last_error_code: str | None
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class TableItem(BaseModel):
    schema_name: str = Field(serialization_alias="schema")
    name: str
    type: Literal["table", "view"]
    column_count: int | None = Field(
        description="Null for BigQuery, which lists tables without their columns."
    )


class ColumnItem(BaseModel):
    name: str
    type: str = Field(description="The type as the database names it.")
    role: Literal["time", "field", "tag"] | None = Field(
        default=None,
        description="What the column is in a time series. Null for every other connection.",
    )

    @classmethod
    def of(cls, column: Column) -> "ColumnItem":
        return cls(name=column.name, type=column.type, role=column.role)


class PreviewRequest(BaseModel, extra="forbid"):
    source: Source


class PreviewData(BaseModel):
    columns: list[ColumnItem]
    rows: list[list[str | None]] = Field(
        description=f"Values as text; one longer than {PREVIEW_CELL_CHARS} characters is cut."
    )
    truncated: bool = Field(description="The source has more rows than were returned.")


class LiveRequest(BaseModel, extra="forbid"):
    name: Identifier = Field(description="The metric or the measurement.")
    fields: FieldNames = Field(
        default_factory=list, description="As in a `timeseries` source: empty for Prometheus."
    )
    tags: TagNames = Field(default_factory=list)
    aggregate: Aggregate
    last: LiveWindow = Field(
        description=(
            "How far back to look. The bucket follows from it: `15s` for `15m`, `1m` for "
            "`1h`, `5m` for `6h` and `15m` for `24h`."
        )
    )

    @model_validator(mode="after")
    def _distinct_names(self) -> "LiveRequest":
        check_series_names(self.fields, self.tags)
        return self


class LiveData(BaseModel):
    columns: list[ColumnItem]
    rows: list[list[str | None]] = Field(
        description="Values as text, ordered by `time` and then by the tags."
    )
    start: datetime = Field(description="The start of the first bucket, in UTC.")
    end: datetime = Field(
        description="The end of the last bucket: the last one that had ended when asked."
    )
    bucket: str
    truncated: bool = Field(
        description=(
            f"There were more than {LIVE_MAX_ROWS} rows and the latest are missing. "
            "Ask for fewer tags."
        )
    )


class GooglePickerCredentials(BaseModel):
    access_token: str = Field(repr=False)
    api_key: str = Field(repr=False)
    app_id: str
