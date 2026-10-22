defmodule SymphonyElixir.GitHubProjects.Adapter do
  @moduledoc """
  GitHub Projects adapter backed by the configured host controller.

  The controller owns tracker reads, workspace preparation and recovery records.
  Codex owns Git and PR operations. Symphony owns scheduling and retries.
  """

  @behaviour SymphonyElixir.Tracker

  alias SymphonyElixir.Config
  alias SymphonyElixir.GitHub.AgentTool
  alias SymphonyElixir.Tracker.Issue

  @report_schema %{
    "type" => "object",
    "additionalProperties" => false,
    "required" => ["summary", "blocked", "blocker", "checks"],
    "properties" => %{
      "browser_capture_ids" => %{
        "type" => "array",
        "maxItems" => 20,
        "uniqueItems" => true,
        "items" => %{"type" => "string", "pattern" => "^[0-9a-f]{32}$"}
      },
      "artifacts" => %{
        "type" => "array",
        "maxItems" => 20,
        "items" => %{
          "type" => "object",
          "additionalProperties" => false,
          "required" => ["path", "sha256", "caption"],
          "properties" => %{
            "path" => %{"type" => "string"},
            "sha256" => %{"type" => "string", "pattern" => "^[0-9a-f]{64}$"},
            "caption" => %{"type" => "string", "maxLength" => 500}
          }
        }
      },
      "summary" => %{"type" => "string"},
      "blocked" => %{"type" => "boolean"},
      "blocker" => %{"type" => "string"},
      "checks" => %{
        "type" => "array",
        "minItems" => 1,
        "items" => %{
          "type" => "object",
          "additionalProperties" => false,
          "required" => ["command", "result", "details"],
          "properties" => %{
            "command" => %{"type" => "string"},
            "result" => %{"type" => "string", "enum" => ["passed", "failed", "not_run"]},
            "details" => %{"type" => "string"}
          }
        }
      }
    }
  }

  @impl true
  def validate_config(settings) do
    provider = settings.provider

    if is_binary(provider["repo"]) and is_binary(provider["controller"]) and
         File.regular?(provider["controller"]) and is_map(provider["roles"]) and
         settings.active_states != [] and settings.terminal_states != [] do
      :ok
    else
      {:error, :invalid_github_projects_configuration}
    end
  end

  @impl true
  def fetch_issues_by_states(states), do: fetch("list", %{"states" => states})

  @impl true
  def fetch_issues_by_ids(ids), do: fetch("refresh", %{"ids" => ids})

  @impl true
  def secret_environment_names(_settings) do
    ["GITHUB_TOKEN", "GH_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_ENTERPRISE_TOKEN"]
  end

  @impl true
  def agent_tool_specs do
    specs =
      AgentTool.tool_specs() ++
        [
          %{
            "name" => "project_claim",
            "description" =>
              "Confirm this session's the configured project claim and read its issue discussion, toolchain paths, previous report and current PR/check context. Codex owns native Git and PR publication.",
            "inputSchema" => %{"type" => "object", "properties" => %{}, "additionalProperties" => false}
          },
          %{
            "name" => "project_browser",
            "description" =>
              "Build/start this issue's full local application, inspect status, record its registered acceptance scenario, or stop it. Builds and recordings run in background jobs. Use completed capture IDs in project_evidence.",
            "inputSchema" => %{
              "type" => "object",
              "additionalProperties" => false,
              "required" => ["action"],
              "properties" => %{
                "action" => %{"type" => "string", "enum" => ["start", "status", "record", "stop"]},
                "scenario" => %{"type" => "string"}
              }
            }
          },
          %{
            "name" => "project_evidence",
            "description" => "Validate and publish completed full-app capture IDs and image artifacts in this issue discussion. This does not commit, push or publish a PR.",
            "inputSchema" => @report_schema
          },
          tool("project_workpad", "Replace the current workpad with its complete maintained plan, acceptance criteria, validation and notes.", %{"body" => %{"type" => "string"}}, ["body"]),
          tool("project_restart", "Acknowledge human Rework after closing the old PR and deleting the old workpad.", %{}, []),
          tool("project_git", "Fetch the configured base or push the validated selected issue branch using host Git credentials.", %{"action" => %{"type" => "string", "enum" => ["fetch", "push"]}}, [
            "action"
          ]),
          tool("project_validate", "Run independent credential, frozen-input and repository checks before a native Git push.", %{}, []),
          tool(
            "project_review",
            "Record the selected current-head draft PR and move to review after required CI and evidence pass.",
            %{"pr_number" => %{"type" => "integer"}, "sha" => %{"type" => "string"}},
            ["pr_number", "sha"]
          ),
          tool("project_done", "Close issue and move to Done after human-triggered landing is confirmed merged.", %{}, []),
          tool("project_block", "Record an external blocker in the maintained workpad and wait in review.", %{"reason" => %{"type" => "string"}}, ["reason"])
        ]

    if Config.settings!().tracker.provider["browser_enabled"] do
      specs
    else
      Enum.reject(specs, &(&1["name"] == "project_browser"))
    end
  end

  @impl true
  def execute_agent_tool(tool, arguments, opts)
      when tool in [
             "project_claim",
             "project_browser",
             "project_evidence",
             "project_workpad",
             "project_restart",
             "project_validate",
             "project_git",
             "project_review",
             "project_done",
             "project_block",
             "github_api"
           ] and
             is_map(arguments) do
    settings = Keyword.fetch!(opts, :tracker_settings)

    case {Keyword.get(opts, :issue), Keyword.get(opts, :workspace)} do
      {%Issue{id: id}, workspace} when is_binary(id) and is_binary(workspace) ->
        params = %{"name" => tool, "arguments" => arguments, "item_id" => id, "workspace" => workspace}

        execute_bound(tool, arguments, params, settings)

      _ ->
        tool_response({:error, :missing_session_context})
    end
  end

  def execute_agent_tool(_tool, _arguments, _opts), do: tool_response({:error, :unsupported_project_tool})

  defp execute_bound("github_api", arguments, params, settings) do
    client = fn method, path, query, body, _opts ->
      native = Map.put(params, "arguments", %{"method" => method, "path" => path, "params" => query, "body" => body})
      native_response(request("tool", native, settings))
    end

    AgentTool.execute("github_api", arguments, github_client: client, tracker_settings: settings)
  end

  defp execute_bound(_tool, _arguments, params, settings), do: tool_response(request("tool", params, settings))
  defp native_response({:ok, %{"status" => status, "body" => payload}}), do: {:ok, %{status: status, body: payload}}
  defp native_response({:error, _reason} = error), do: error

  defp tool(name, description, properties, required) do
    %{"name" => name, "description" => description, "inputSchema" => %{"type" => "object", "additionalProperties" => false, "properties" => properties, "required" => required}}
  end

  @impl true
  def dispatch_order_key(%Issue{native_ref: ref, priority: priority}) when is_map(ref) do
    {ref["execution_order"] || 9_223_372_036_854_775_807, priority || 5, ref["number"] || 0}
  end

  def dispatch_order_key(_issue), do: {9_223_372_036_854_775_807, 5, 0}

  defp fetch(method, params) do
    case request(method, params, Config.settings!().tracker) do
      {:ok, rows} when is_list(rows) -> {:ok, Enum.map(rows, &normalize/1)}
      {:error, _reason} = error -> error
      _other -> {:error, :invalid_controller_issue_list}
    end
  end

  defp normalize(row) do
    keys = Map.keys(%Issue{}) -- [:__struct__]

    issue =
      Map.new(keys, fn key -> {key, Map.get(row, Atom.to_string(key))} end)
      |> Map.update!(:created_at, &timestamp/1)
      |> Map.update!(:updated_at, &timestamp/1)

    struct!(Issue, issue)
  end

  defp timestamp(value) when is_binary(value) do
    case DateTime.from_iso8601(value) do
      {:ok, datetime, _offset} -> datetime
      _ -> nil
    end
  end

  defp timestamp(_value), do: nil

  defp request(method, params, settings) do
    payload = Jason.encode!(%{"repo" => settings.provider["repo"], "method" => method, "params" => params})

    case System.cmd(settings.provider["controller"], ["rpc", payload], stderr_to_stdout: true) do
      {output, status} -> decode_response(output, status)
    end
  rescue
    error in ErlangError -> {:error, {:controller_unavailable, error.original}}
  end

  defp decode_response(output, status) do
    case Jason.decode(output) do
      {:ok, %{"ok" => true, "result" => result}} when status == 0 -> {:ok, result}
      {:ok, %{"ok" => false, "error" => error}} -> {:error, {:controller_error, error}}
      _ -> {:error, {:invalid_controller_response, status}}
    end
  end

  defp tool_response({:ok, result}), do: tool_result(true, result)
  defp tool_response({:error, reason}), do: tool_result(false, %{"error" => inspect(reason)})

  defp tool_result(success, result) do
    output = Jason.encode!(result)
    %{"success" => success, "output" => output, "contentItems" => [%{"type" => "inputText", "text" => output}]}
  end
end
