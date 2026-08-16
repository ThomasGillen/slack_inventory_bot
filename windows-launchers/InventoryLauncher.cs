using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;

internal static class InventoryLauncher
{
#if INIT_SHEET
    private const string WindowTitle = "Inventory Sheet Setup";
    private const string EntryPointName = "inventory-sheet-init.exe";
#else
    private const string WindowTitle = "Slack Inventory Bot";
    private const string EntryPointName = "inventory-bot.exe";
#endif

    private const string CheckFlag = "--launcher-check";
    private const string NoPauseFlag = "--launcher-no-pause";

    private static int Main(string[] args)
    {
        Console.Title = WindowTitle;
        Console.WriteLine(WindowTitle);
        Console.WriteLine(new string('=', WindowTitle.Length));
        Console.WriteLine();

        string projectRoot = FindProjectRoot();
        Directory.SetCurrentDirectory(projectRoot);

        bool checkOnly = HasArgument(args, CheckFlag);
        bool pause = !HasArgument(args, NoPauseFlag) && !checkOnly;
        string entryPoint = Path.Combine(
            projectRoot, ".venv", "Scripts", EntryPointName
        );

        if (checkOnly)
        {
            return CheckInstallation(projectRoot, entryPoint);
        }

        try
        {
            int setupExitCode = EnsureLocalInstallation(projectRoot, entryPoint);
            if (setupExitCode != 0)
            {
                return Finish(
                    setupExitCode,
                    "The local Python setup could not be completed.",
                    pause
                );
            }

            int configExitCode = EnsureConfiguration(projectRoot);
            if (configExitCode != 0)
            {
                return Finish(configExitCode, "Configuration was not completed.", pause);
            }

            int validationExitCode = ValidateConfiguration(projectRoot);
            if (validationExitCode != 0)
            {
                return Finish(
                    validationExitCode,
                    "Fix the configuration values shown above, then run this launcher again.",
                    pause
                );
            }

            List<string> forwardedArguments = UserArguments(args);
#if INIT_SHEET
            if (forwardedArguments.Count == 0)
            {
                forwardedArguments.AddRange(ChooseSheetAction());
            }
#endif

            Console.WriteLine();
#if INIT_SHEET
            Console.WriteLine("Updating the Google Sheet...");
#else
            Console.WriteLine("Starting the bot. Keep this window open while the bot is running.");
            Console.WriteLine("Press Ctrl+C to stop it.");
#endif
            Console.WriteLine();

            int exitCode = RunProcess(
                entryPoint,
                JoinArguments(forwardedArguments),
                projectRoot
            );
            string result = exitCode == 0
                ? "Finished successfully."
                : "The command stopped with an error. Review the messages above.";
            return Finish(exitCode, result, pause);
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine();
            Console.Error.WriteLine("Unexpected launcher error:");
            Console.Error.WriteLine(exception.Message);
            return Finish(1, null, pause);
        }
    }

    private static string FindProjectRoot()
    {
        DirectoryInfo directory = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
        while (directory != null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "pyproject.toml")))
            {
                return NormalizeDirectory(directory.FullName);
            }
            directory = directory.Parent;
        }

        return NormalizeDirectory(AppDomain.CurrentDomain.BaseDirectory);
    }

    private static string NormalizeDirectory(string path)
    {
        string fullPath = Path.GetFullPath(path);
        string root = Path.GetPathRoot(fullPath);
        if (string.Equals(fullPath, root, StringComparison.OrdinalIgnoreCase))
        {
            return fullPath;
        }
        return fullPath.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    }

    private static int CheckInstallation(string projectRoot, string entryPoint)
    {
        bool projectFound = File.Exists(Path.Combine(projectRoot, "pyproject.toml"));
        bool commandFound = File.Exists(entryPoint);
        bool configFound = File.Exists(Path.Combine(projectRoot, ".env"));

        Console.WriteLine("Project folder: " + projectRoot);
        Console.WriteLine("Project files: " + Status(projectFound));
        Console.WriteLine("Python command: " + Status(commandFound));
        Console.WriteLine("Configuration: " + Status(configFound));
        return projectFound && commandFound && configFound ? 0 : 1;
    }

    private static int EnsureLocalInstallation(string projectRoot, string entryPoint)
    {
        if (!File.Exists(Path.Combine(projectRoot, "pyproject.toml")))
        {
            Console.Error.WriteLine(
                "pyproject.toml was not found. Run this launcher from a complete, current copy of the project."
            );
            return 1;
        }

        string virtualEnvironment = Path.Combine(projectRoot, ".venv");
        string virtualPython = Path.Combine(virtualEnvironment, "Scripts", "python.exe");

        if (!File.Exists(virtualPython))
        {
            Console.WriteLine("First-time setup: creating the local Python environment...");
            int createExitCode;
            if (!TryRunProcess(
                "py.exe",
                "-3 -m venv " + Quote(virtualEnvironment),
                projectRoot,
                out createExitCode
            ))
            {
                if (!TryRunProcess(
                    "python.exe",
                    "-m venv " + Quote(virtualEnvironment),
                    projectRoot,
                    out createExitCode
                ))
                {
                    Console.Error.WriteLine(
                        "Python was not found. Install Python 3.11 or newer, then open this file again."
                    );
                    return 1;
                }
            }

            if (createExitCode != 0 || !File.Exists(virtualPython))
            {
                Console.Error.WriteLine(
                    "Python could not create the .venv folder. Python 3.11 or newer is required."
                );
                return createExitCode == 0 ? 1 : createExitCode;
            }
        }

        int versionExitCode = RunProcess(
            virtualPython,
            "-c \"import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)\"",
            projectRoot
        );
        if (versionExitCode != 0)
        {
            Console.Error.WriteLine(
                "The existing .venv uses an unsupported Python version. Delete .venv, install Python 3.11 or newer, and run this launcher again."
            );
            return 1;
        }

        if (!File.Exists(entryPoint))
        {
            Console.WriteLine("First-time setup: installing the inventory bot...");
            int installExitCode = RunProcess(
                virtualPython,
                "-m pip install -e " + Quote(projectRoot),
                projectRoot
            );
            if (installExitCode != 0 || !File.Exists(entryPoint))
            {
                Console.Error.WriteLine(
                    "The inventory bot could not be installed. Check the messages above and your internet connection."
                );
                return installExitCode == 0 ? 1 : installExitCode;
            }
        }

        return 0;
    }

    private static int ValidateConfiguration(string projectRoot)
    {
        string environmentFile = Path.Combine(projectRoot, ".env");
        Dictionary<string, string> values = ReadEnvironmentFile(environmentFile);
        bool valid = true;

        string spreadsheetId = ConfiguredValue(values, "GOOGLE_SPREADSHEET_ID");
        if (string.IsNullOrWhiteSpace(spreadsheetId)
            || IsPlaceholder(spreadsheetId, "your-spreadsheet-id"))
        {
            Console.Error.WriteLine(
                "GOOGLE_SPREADSHEET_ID is missing. Copy the value between /d/ and /edit in the Google Sheet URL into .env."
            );
            valid = false;
        }

        string keyFile = ConfiguredValue(values, "GOOGLE_SERVICE_ACCOUNT_FILE");
        string inlineJson = ConfiguredValue(values, "GOOGLE_SERVICE_ACCOUNT_JSON");
        if (!string.IsNullOrWhiteSpace(keyFile) && !string.IsNullOrWhiteSpace(inlineJson))
        {
            Console.Error.WriteLine(
                "Set either GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON, not both."
            );
            valid = false;
        }
        else if (!string.IsNullOrWhiteSpace(keyFile))
        {
            try
            {
                string resolvedKeyFile = ResolveConfiguredPath(keyFile, projectRoot);
                if (Directory.Exists(resolvedKeyFile))
                {
                    Console.Error.WriteLine(
                        "GOOGLE_SERVICE_ACCOUNT_FILE points to a folder. Set it to the downloaded service-account .json file:"
                    );
                    Console.Error.WriteLine(resolvedKeyFile);
                    valid = false;
                }
                else if (!File.Exists(resolvedKeyFile))
                {
                    Console.Error.WriteLine(
                        "GOOGLE_SERVICE_ACCOUNT_FILE could not be found:"
                    );
                    Console.Error.WriteLine(resolvedKeyFile);
                    valid = false;
                }
                else if (!string.Equals(
                    Path.GetExtension(resolvedKeyFile),
                    ".json",
                    StringComparison.OrdinalIgnoreCase
                ))
                {
                    Console.Error.WriteLine(
                        "GOOGLE_SERVICE_ACCOUNT_FILE must point to the downloaded service-account .json file:"
                    );
                    Console.Error.WriteLine(resolvedKeyFile);
                    valid = false;
                }
            }
            catch (Exception exception)
            {
                Console.Error.WriteLine(
                    "GOOGLE_SERVICE_ACCOUNT_FILE is not a valid Windows file path: "
                    + exception.Message
                );
                valid = false;
            }
        }
        else if (string.IsNullOrWhiteSpace(inlineJson))
        {
            Console.WriteLine(
                "Google credential file: not set; Application Default Credentials will be used."
            );
        }

#if !INIT_SHEET
        string botToken = ConfiguredValue(values, "SLACK_BOT_TOKEN");
        if (string.IsNullOrWhiteSpace(botToken)
            || IsPlaceholder(botToken, "xoxb-your-bot-token")
            || !botToken.StartsWith("xoxb-", StringComparison.Ordinal))
        {
            Console.Error.WriteLine(
                "SLACK_BOT_TOKEN must be the Bot User OAuth Token beginning with xoxb-."
            );
            valid = false;
        }

        string appToken = ConfiguredValue(values, "SLACK_APP_TOKEN");
        if (string.IsNullOrWhiteSpace(appToken)
            || IsPlaceholder(appToken, "xapp-your-socket-mode-token")
            || IsPlaceholder(appToken, "xapp-your-app-token")
            || !appToken.StartsWith("xapp-", StringComparison.Ordinal))
        {
            Console.Error.WriteLine(
                "SLACK_APP_TOKEN must be an app-level Socket Mode token beginning with xapp-."
            );
            valid = false;
        }
#endif

        if (valid)
        {
            Console.WriteLine("Configuration preflight: OK");
            return 0;
        }
        return 1;
    }

    private static Dictionary<string, string> ReadEnvironmentFile(string path)
    {
        Dictionary<string, string> values = new Dictionary<string, string>(
            StringComparer.OrdinalIgnoreCase
        );
        foreach (string rawLine in File.ReadAllLines(path))
        {
            string line = rawLine.Trim();
            if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
            {
                continue;
            }
            int separator = line.IndexOf('=');
            if (separator < 1)
            {
                continue;
            }
            string key = line.Substring(0, separator).Trim();
            string value = line.Substring(separator + 1).Trim();
            if (value.Length >= 2
                && ((value[0] == '"' && value[value.Length - 1] == '"')
                    || (value[0] == '\'' && value[value.Length - 1] == '\'')))
            {
                value = value.Substring(1, value.Length - 2);
            }
            values[key] = value;
        }
        return values;
    }

    private static string ConfiguredValue(
        Dictionary<string, string> values,
        string name
    )
    {
        string processValue = Environment.GetEnvironmentVariable(name);
        if (!string.IsNullOrWhiteSpace(processValue))
        {
            return processValue.Trim();
        }
        string fileValue;
        return values.TryGetValue(name, out fileValue) ? fileValue.Trim() : string.Empty;
    }

    private static bool IsPlaceholder(string value, string placeholder)
    {
        return string.Equals(value.Trim(), "...", StringComparison.OrdinalIgnoreCase)
            || value.Trim().EndsWith("...", StringComparison.Ordinal)
            || string.Equals(value.Trim(), placeholder, StringComparison.OrdinalIgnoreCase);
    }

    private static string ResolveConfiguredPath(string value, string projectRoot)
    {
        string expanded = Environment.ExpandEnvironmentVariables(value.Trim());
        if (expanded == "~")
        {
            expanded = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }
        else if (expanded.StartsWith("~\\", StringComparison.Ordinal)
            || expanded.StartsWith("~/", StringComparison.Ordinal))
        {
            expanded = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                expanded.Substring(2)
            );
        }
        if (!Path.IsPathRooted(expanded))
        {
            expanded = Path.Combine(projectRoot, expanded);
        }
        return Path.GetFullPath(expanded);
    }

    private static int EnsureConfiguration(string projectRoot)
    {
        string environmentFile = Path.Combine(projectRoot, ".env");
        if (File.Exists(environmentFile))
        {
            return 0;
        }

        string exampleFile = Path.Combine(projectRoot, ".env.example");
        if (!File.Exists(exampleFile))
        {
            Console.Error.WriteLine(
                "The .env configuration file is missing, and .env.example was not found."
            );
            return 1;
        }

        File.Copy(exampleFile, environmentFile);
        Console.WriteLine();
        Console.WriteLine("A new .env configuration file was created.");
        Console.WriteLine("It will open in Notepad. Fill in the settings, save it, and close Notepad.");
        Console.WriteLine();

        try
        {
            Process editor = Process.Start("notepad.exe", Quote(environmentFile));
            if (editor != null)
            {
                editor.WaitForExit();
            }
        }
        catch (Win32Exception)
        {
            Console.Error.WriteLine("Notepad could not be opened. Edit this file, then try again:");
            Console.Error.WriteLine(environmentFile);
            return 1;
        }

        return 0;
    }

#if INIT_SHEET
    private static IEnumerable<string> ChooseSheetAction()
    {
        Console.WriteLine();
        Console.WriteLine("Choose what to do:");
        Console.WriteLine("  1. Initialize or verify the current sheet (recommended)");
        Console.WriteLine("  2. Migrate an older Items tab");
        Console.WriteLine("  3. Migrate an older Reservations tab");
        Console.WriteLine("  4. Migrate both older tabs");
        Console.Write("Selection [1]: ");

        string selection = Console.ReadLine();
        switch ((selection ?? string.Empty).Trim())
        {
            case "2":
                return new[] { "--migrate-items" };
            case "3":
                return new[] { "--migrate-reservations" };
            case "4":
                return new[] { "--migrate-items", "--migrate-reservations" };
            default:
                return new string[0];
        }
    }
#endif

    private static bool TryRunProcess(
        string fileName,
        string arguments,
        string workingDirectory,
        out int exitCode
    )
    {
        try
        {
            exitCode = RunProcess(fileName, arguments, workingDirectory);
            return true;
        }
        catch (Win32Exception)
        {
            exitCode = 1;
            return false;
        }
    }

    private static int RunProcess(
        string fileName,
        string arguments,
        string workingDirectory
    )
    {
        ProcessStartInfo startInfo = new ProcessStartInfo();
        startInfo.FileName = fileName;
        startInfo.Arguments = arguments;
        startInfo.WorkingDirectory = workingDirectory;
        startInfo.UseShellExecute = false;

        using (Process process = Process.Start(startInfo))
        {
            if (process == null)
            {
                throw new InvalidOperationException("Windows could not start " + fileName + ".");
            }
            process.WaitForExit();
            return process.ExitCode;
        }
    }

    private static List<string> UserArguments(string[] args)
    {
        List<string> userArguments = new List<string>();
        foreach (string argument in args)
        {
            if (!string.Equals(argument, CheckFlag, StringComparison.OrdinalIgnoreCase)
                && !string.Equals(argument, NoPauseFlag, StringComparison.OrdinalIgnoreCase))
            {
                userArguments.Add(argument);
            }
        }
        return userArguments;
    }

    private static bool HasArgument(string[] args, string expected)
    {
        foreach (string argument in args)
        {
            if (string.Equals(argument, expected, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static string JoinArguments(IEnumerable<string> arguments)
    {
        List<string> quoted = new List<string>();
        foreach (string argument in arguments)
        {
            quoted.Add(Quote(argument));
        }
        return string.Join(" ", quoted.ToArray());
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static string Status(bool found)
    {
        return found ? "OK" : "MISSING";
    }

    private static int Finish(int exitCode, string message, bool pause)
    {
        if (!string.IsNullOrEmpty(message))
        {
            Console.WriteLine();
            Console.WriteLine(message);
        }
        if (pause)
        {
            Console.WriteLine();
            Console.Write("Press Enter to close this window.");
            Console.ReadLine();
        }
        return exitCode;
    }
}
