using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

namespace HermesVerifierJobHost
{
    public static class Controller
    {
        private const int ProtocolVersion = 1;
        private const uint CreateSuspended = 0x00000004;
        private const uint CreateUnicodeEnvironment = 0x00000400;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const int JobObjectBasicAccountingInformation = 1;
        private const int JobObjectExtendedLimitInformation = 9;
        private const uint Infinite = 0xFFFFFFFF;
        private const uint WaitObject0 = 0x00000000;
        private const uint ResumeFailure = 0xFFFFFFFF;
        private const int ErrorExitCode = unchecked((int)0xE0434F4D);
        private const int MaxProtocolStringLength = 32767;
        private const int MaxProtocolCollectionCount = 4096;
        private const int MaxErrorMessageLength = 8192;

        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        private static readonly object OutputLock = new object();

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public uint cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public uint dwX;
            public uint dwY;
            public uint dwXSize;
            public uint dwYSize;
            public uint dwXCountChars;
            public uint dwYCountChars;
            public uint dwFillAttribute;
            public uint dwFlags;
            public ushort wShowWindow;
            public ushort cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public uint dwProcessId;
            public uint dwThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct FILETIME
        {
            public uint dwLowDateTime;
            public uint dwHighDateTime;
        }

        private sealed class LaunchRequest
        {
            public string Nonce;
            public string Executable;
            public string[] Arguments;
            public string WorkingDirectory;
            public Dictionary<string, string> Environment;
            public int TerminationTimeoutMs;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObjectW(IntPtr jobAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcessW(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string currentDirectory,
            ref STARTUPINFO startupInfo,
            out PROCESS_INFORMATION processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength,
            IntPtr returnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessTimes(
            IntPtr process,
            out FILETIME creationTime,
            out FILETIME exitTime,
            out FILETIME kernelTime,
            out FILETIME userTime);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryFullProcessImageNameW(
            IntPtr process,
            uint flags,
            StringBuilder executableName,
            ref uint size);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        public static int Run()
        {
            IntPtr job = IntPtr.Zero;
            PROCESS_INFORMATION process = new PROCESS_INFORMATION();
            bool processCreated = false;
            bool assigned = false;
            string nonce = null;
            ManualResetEvent targetExitWritten = null;

            try
            {
                string launchLine = Console.In.ReadLine();
                if (launchLine == null)
                {
                    Console.Error.WriteLine("Windows verifier Job host received EOF before launch data");
                    return 1;
                }

                LaunchRequest request = ParseLaunchRequest(launchLine);
                nonce = request.Nonce;

                job = CreateJobObjectW(IntPtr.Zero, null);
                EnsureHandle(job, "CreateJobObjectW");
                ConfigureKillOnClose(job);

                process = CreateSuspendedProcess(request);
                processCreated = true;

                string actualExecutable = ReadExecutableIdentity(process.hProcess);
                ulong creationTime = ReadCreationTime(process.hProcess);
                if (!string.Equals(
                    Path.GetFullPath(actualExecutable),
                    request.Executable,
                    StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidOperationException(
                        "created target executable identity did not match the requested absolute executable");
                }

                if (!AssignProcessToJobObject(job, process.hProcess))
                {
                    int assignmentError = Marshal.GetLastWin32Error();
                    TerminateSuspendedPreAssignment(process.hProcess);
                    throw NativeFailure("AssignProcessToJobObject", assignmentError);
                }
                assigned = true;

                uint resumeResult = ResumeThread(process.hThread);
                if (resumeResult == ResumeFailure)
                {
                    int resumeError = Marshal.GetLastWin32Error();
                    TerminateAssignedJob(job);
                    WaitForZeroActive(job, request.TerminationTimeoutMs);
                    throw NativeFailure("ResumeThread", resumeError);
                }

                WriteRecord(new Dictionary<string, object>
                {
                    { "v", ProtocolVersion },
                    { "type", "launched" },
                    { "nonce", nonce },
                    { "target", new Dictionary<string, object>
                        {
                            { "pid", process.dwProcessId },
                            { "creationTime100ns", creationTime.ToString() },
                            { "executable", actualExecutable }
                        }
                    }
                });

                targetExitWritten = StartExitNotification(
                    process.hProcess,
                    nonce,
                    process.dwProcessId);
                return CommandLoop(
                    job,
                    process.hProcess,
                    process.dwProcessId,
                    request,
                    nonce,
                    targetExitWritten);
            }
            catch (Exception error)
            {
                if (processCreated && !assigned && process.hProcess != IntPtr.Zero)
                {
                    TerminateSuspendedPreAssignment(process.hProcess);
                }

                if (nonce != null)
                {
                    WriteError(nonce, "controller", error);
                }
                else
                {
                    Console.Error.WriteLine("Windows verifier Job host failed: " + error.Message);
                }
                return 1;
            }
            finally
            {
                if (process.hThread != IntPtr.Zero)
                {
                    CloseHandle(process.hThread);
                }
                if (process.hProcess != IntPtr.Zero)
                {
                    CloseHandle(process.hProcess);
                }
                if (job != IntPtr.Zero)
                {
                    CloseHandle(job);
                }
            }
        }

        private static int CommandLoop(
            IntPtr job,
            IntPtr targetProcess,
            uint targetPid,
            LaunchRequest request,
            string nonce,
            ManualResetEvent targetExitWritten)
        {
            bool terminationIssued = false;
            int terminationCount = 0;
            int cleanupRequestCount = 0;
            Dictionary<string, object> cleanupReceipt = null;
            string line;

            while ((line = Console.In.ReadLine()) != null)
            {
                Dictionary<string, object> command;
                try
                {
                    command = DeserializeObject(line, "command");
                    RequireExactKeys(command, "command", "v", "type", "nonce");
                    RequireProtocolVersion(command);
                    string commandType = RequireString(command, "type", "command", 16);
                    string commandNonce = RequireString(command, "nonce", "command", 36);
                    if (!string.Equals(commandNonce, nonce, StringComparison.Ordinal))
                    {
                        throw new InvalidDataException("command nonce did not match the launch nonce");
                    }
                    if (string.Equals(commandType, "status", StringComparison.Ordinal))
                    {
                        if (terminationIssued)
                        {
                            throw new InvalidOperationException(
                                "target identity cannot be sampled after cleanup started");
                        }
                        WriteRecord(new Dictionary<string, object>
                        {
                            { "v", ProtocolVersion },
                            { "type", "status" },
                            { "nonce", nonce },
                            { "activeProcesses", QueryAccounting(job).ActiveProcesses },
                            { "target", new Dictionary<string, object>
                                {
                                    { "pid", targetPid },
                                    { "creationTime100ns", ReadCreationTime(targetProcess).ToString() },
                                    { "executable", ReadExecutableIdentity(targetProcess) }
                                }
                            }
                        });
                        continue;
                    }
                    if (!string.Equals(commandType, "cleanup", StringComparison.Ordinal))
                    {
                        throw new InvalidDataException("unsupported controller command type");
                    }
                }
                catch (Exception error)
                {
                    WriteError(nonce, "protocol", error);
                    return 1;
                }

                try
                {
                    cleanupRequestCount++;
                    if (!terminationIssued)
                    {
                        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION beforeTermination =
                            QueryAccounting(job);
                        TerminateAssignedJob(job);
                        terminationIssued = true;
                        terminationCount++;
                        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting =
                            WaitForZeroActive(job, request.TerminationTimeoutMs);
                        cleanupReceipt = new Dictionary<string, object>
                        {
                            { "v", ProtocolVersion },
                            { "type", "cleaned" },
                            { "nonce", nonce },
                            { "activeProcessesBeforeTerminate", beforeTermination.ActiveProcesses },
                            { "activeProcesses", 0 },
                            { "totalProcesses", accounting.TotalProcesses },
                            { "terminationCount", terminationCount },
                            { "cleanupRequestCount", cleanupRequestCount }
                        };
                    }

                    if (!targetExitWritten.WaitOne(request.TerminationTimeoutMs))
                    {
                        throw new TimeoutException(
                            "target-exit notification was not written before cleanup acknowledgement");
                    }
                    WriteRecord(cleanupReceipt);
                }
                catch (Exception error)
                {
                    WriteError(nonce, "cleanup", error);
                    return 1;
                }
            }

            return 0;
        }

        private static LaunchRequest ParseLaunchRequest(string line)
        {
            Dictionary<string, object> record = DeserializeObject(line, "launch record");
            RequireExactKeys(
                record,
                "launch record",
                "v",
                "type",
                "nonce",
                "executable",
                "args",
                "cwd",
                "environment",
                "terminationTimeoutMs");
            RequireProtocolVersion(record);

            if (!string.Equals(
                RequireString(record, "type", "launch record", 16),
                "launch",
                StringComparison.Ordinal))
            {
                throw new InvalidDataException("first protocol record must be a launch record");
            }

            string nonce = RequireString(record, "nonce", "launch record", 36);
            Guid parsedNonce;
            if (!Guid.TryParseExact(nonce, "D", out parsedNonce) ||
                !string.Equals(parsedNonce.ToString("D"), nonce, StringComparison.Ordinal))
            {
                throw new InvalidDataException("launch nonce must be a canonical UUID");
            }

            string executable = RequireAbsoluteExistingFile(
                RequireString(record, "executable", "launch record"),
                "executable");
            string workingDirectory = RequireAbsoluteExistingDirectory(
                RequireString(record, "cwd", "launch record"),
                "cwd");
            string[] arguments = RequireStringArray(record, "args");
            Dictionary<string, string> environment = RequireEnvironment(record, "environment");
            int timeout = RequireInteger(record, "terminationTimeoutMs");
            if (timeout < 100 || timeout > 600000)
            {
                throw new InvalidDataException("terminationTimeoutMs must be between 100 and 600000");
            }

            return new LaunchRequest
            {
                Nonce = nonce,
                Executable = executable,
                Arguments = arguments,
                WorkingDirectory = workingDirectory,
                Environment = environment,
                TerminationTimeoutMs = timeout
            };
        }

        private static PROCESS_INFORMATION CreateSuspendedProcess(LaunchRequest request)
        {
            STARTUPINFO startup = new STARTUPINFO();
            startup.cb = (uint)Marshal.SizeOf(typeof(STARTUPINFO));
            PROCESS_INFORMATION process;
            IntPtr environment = IntPtr.Zero;

            try
            {
                environment = Marshal.StringToHGlobalUni(BuildEnvironmentBlock(request.Environment));
                StringBuilder commandLine = new StringBuilder(BuildCommandLine(request.Executable, request.Arguments));
                bool created = CreateProcessW(
                    request.Executable,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    false,
                    CreateSuspended | CreateUnicodeEnvironment,
                    environment,
                    request.WorkingDirectory,
                    ref startup,
                    out process);
                if (!created)
                {
                    throw NativeFailure("CreateProcessW", Marshal.GetLastWin32Error());
                }
                return process;
            }
            finally
            {
                if (environment != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(environment);
                }
            }
        }

        private static void ConfigureKillOnClose(IntPtr job)
        {
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, buffer, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buffer, (uint)size))
                {
                    throw NativeFailure("SetInformationJobObject", Marshal.GetLastWin32Error());
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        private static JOBOBJECT_BASIC_ACCOUNTING_INFORMATION QueryAccounting(IntPtr job)
        {
            int size = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                if (!QueryInformationJobObject(
                    job,
                    JobObjectBasicAccountingInformation,
                    buffer,
                    (uint)size,
                    IntPtr.Zero))
                {
                    throw NativeFailure("QueryInformationJobObject", Marshal.GetLastWin32Error());
                }
                JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting =
                    (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)Marshal.PtrToStructure(
                        buffer,
                        typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
                return accounting;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }

        private static JOBOBJECT_BASIC_ACCOUNTING_INFORMATION WaitForZeroActive(
            IntPtr job,
            int timeoutMs)
        {
            long started = Stopwatch.GetTimestamp();
            double elapsedMilliseconds;
            do
            {
                JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting = QueryAccounting(job);
                if (accounting.ActiveProcesses == 0)
                {
                    return accounting;
                }
                Thread.Sleep(10);
                elapsedMilliseconds =
                    (Stopwatch.GetTimestamp() - started) * 1000.0 / Stopwatch.Frequency;
            }
            while (elapsedMilliseconds <= timeoutMs);

            throw new TimeoutException(
                "Windows verifier Job cleanup timed out before ActiveProcesses reached zero");
        }

        private static void TerminateAssignedJob(IntPtr job)
        {
            if (!TerminateJobObject(job, unchecked((uint)ErrorExitCode)))
            {
                throw NativeFailure("TerminateJobObject", Marshal.GetLastWin32Error());
            }
        }

        private static void TerminateSuspendedPreAssignment(IntPtr process)
        {
            if (process == IntPtr.Zero)
            {
                return;
            }

            TerminateProcess(process, unchecked((uint)ErrorExitCode));
            WaitForSingleObject(process, 5000);
        }

        private static ulong ReadCreationTime(IntPtr process)
        {
            FILETIME creation;
            FILETIME exit;
            FILETIME kernel;
            FILETIME user;
            if (!GetProcessTimes(process, out creation, out exit, out kernel, out user))
            {
                throw NativeFailure("GetProcessTimes", Marshal.GetLastWin32Error());
            }
            return ((ulong)creation.dwHighDateTime << 32) | creation.dwLowDateTime;
        }

        private static string ReadExecutableIdentity(IntPtr process)
        {
            uint capacity = 32768;
            StringBuilder path = new StringBuilder((int)capacity);
            if (!QueryFullProcessImageNameW(process, 0, path, ref capacity))
            {
                throw NativeFailure("QueryFullProcessImageNameW", Marshal.GetLastWin32Error());
            }
            return Path.GetFullPath(path.ToString());
        }

        private static ManualResetEvent StartExitNotification(
            IntPtr process,
            string nonce,
            uint pid)
        {
            ManualResetEvent written = new ManualResetEvent(false);
            ThreadPool.QueueUserWorkItem(delegate
            {
                uint wait = WaitForSingleObject(process, Infinite);
                if (wait == WaitObject0)
                {
                    WriteRecord(new Dictionary<string, object>
                    {
                        { "v", ProtocolVersion },
                        { "type", "target_exit" },
                        { "nonce", nonce },
                        { "targetPid", pid }
                    });
                    written.Set();
                }
            });
            return written;
        }

        private static string BuildCommandLine(string executable, string[] arguments)
        {
            StringBuilder commandLine = new StringBuilder(QuoteArgument(executable));
            foreach (string argument in arguments)
            {
                commandLine.Append(' ');
                commandLine.Append(QuoteArgument(argument));
            }
            return commandLine.ToString();
        }

        private static string QuoteArgument(string argument)
        {
            if (argument.Length > 0 && argument.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
            {
                return argument;
            }

            StringBuilder quoted = new StringBuilder("\"");
            int backslashes = 0;
            foreach (char character in argument)
            {
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }

                if (character == '"')
                {
                    quoted.Append('\\', backslashes * 2 + 1);
                    quoted.Append('"');
                    backslashes = 0;
                    continue;
                }

                quoted.Append('\\', backslashes);
                backslashes = 0;
                quoted.Append(character);
            }
            quoted.Append('\\', backslashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }

        private static string BuildEnvironmentBlock(Dictionary<string, string> environment)
        {
            List<string> names = new List<string>(environment.Keys);
            names.Sort(StringComparer.OrdinalIgnoreCase);
            StringBuilder block = new StringBuilder();
            foreach (string name in names)
            {
                if (name.Length == 0 || name.IndexOf('=') >= 0 || name.IndexOf('\0') >= 0)
                {
                    throw new InvalidDataException("environment contains an invalid variable name");
                }
                string value = environment[name];
                if (value.IndexOf('\0') >= 0)
                {
                    throw new InvalidDataException("environment contains a NUL value");
                }
                block.Append(name);
                block.Append('=');
                block.Append(value);
                block.Append('\0');
            }
            block.Append('\0');
            return block.ToString();
        }

        private static Dictionary<string, object> DeserializeObject(string line, string label)
        {
            object parsed;
            try
            {
                parsed = Json.DeserializeObject(line);
            }
            catch (Exception error)
            {
                throw new InvalidDataException(label + " is not valid JSON", error);
            }
            Dictionary<string, object> record = parsed as Dictionary<string, object>;
            if (record == null)
            {
                throw new InvalidDataException(label + " must be a JSON object");
            }
            return record;
        }

        private static void RequireExactKeys(
            Dictionary<string, object> record,
            string label,
            params string[] keys)
        {
            HashSet<string> expected = new HashSet<string>(keys, StringComparer.Ordinal);
            foreach (string key in record.Keys)
            {
                if (!expected.Remove(key))
                {
                    throw new InvalidDataException(label + " contains unsupported field: " + key);
                }
            }
            if (expected.Count != 0)
            {
                throw new InvalidDataException(label + " is missing field: " + First(expected));
            }
        }

        private static string First(HashSet<string> values)
        {
            foreach (string value in values)
            {
                return value;
            }
            return "unknown";
        }

        private static void RequireProtocolVersion(Dictionary<string, object> record)
        {
            if (RequireInteger(record, "v") != ProtocolVersion)
            {
                throw new InvalidDataException("unsupported Windows verifier Job protocol version");
            }
        }

        private static string RequireString(
            Dictionary<string, object> record,
            string key,
            string label,
            int maxLength)
        {
            object value;
            string text;
            if (!record.TryGetValue(key, out value) ||
                (text = value as string) == null ||
                text.Length == 0 ||
                text.Length > maxLength ||
                text.IndexOf('\0') >= 0)
            {
                throw new InvalidDataException(
                    label + " field " + key + " must be a bounded non-empty NUL-free string");
            }
            return text;
        }

        private static string RequireString(
            Dictionary<string, object> record,
            string key,
            string label)
        {
            return RequireString(record, key, label, MaxProtocolStringLength);
        }

        private static int RequireInteger(Dictionary<string, object> record, string key)
        {
            object value;
            if (!record.TryGetValue(key, out value) || !(value is int))
            {
                throw new InvalidDataException("field " + key + " must be a JSON integer");
            }
            return (int)value;
        }

        private static string[] RequireStringArray(Dictionary<string, object> record, string key)
        {
            object value;
            if (!record.TryGetValue(key, out value) || !(value is object[]))
            {
                throw new InvalidDataException("field " + key + " must be an array of strings");
            }
            object[] input = (object[])value;
            if (input.Length > MaxProtocolCollectionCount)
            {
                throw new InvalidDataException("field " + key + " contains too many arguments");
            }

            List<string> result = new List<string>(input.Length);
            long totalLength = 0;
            foreach (object item in input)
            {
                string text = item as string;
                if (text == null ||
                    text.Length > MaxProtocolStringLength ||
                    text.IndexOf('\0') >= 0)
                {
                    throw new InvalidDataException(
                        "field " + key + " must contain only bounded NUL-free strings");
                }
                totalLength += text.Length;
                if (totalLength > MaxProtocolStringLength)
                {
                    throw new InvalidDataException("field " + key + " exceeds the protocol size bound");
                }
                result.Add(text);
            }
            return result.ToArray();
        }

        private static Dictionary<string, string> RequireEnvironment(
            Dictionary<string, object> record,
            string key)
        {
            object value;
            if (!record.TryGetValue(key, out value))
            {
                throw new InvalidDataException("missing environment object");
            }
            Dictionary<string, object> input = value as Dictionary<string, object>;
            if (input == null)
            {
                throw new InvalidDataException("environment must be a JSON object");
            }
            Dictionary<string, string> result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            long totalLength = 1;
            foreach (KeyValuePair<string, object> entry in input)
            {
                string text = entry.Value as string;
                if (entry.Key.Length == 0 ||
                    entry.Key.Length > MaxProtocolStringLength ||
                    entry.Key.IndexOf('=') >= 0 ||
                    entry.Key.IndexOf('\0') >= 0 ||
                    text == null ||
                    text.Length > MaxProtocolStringLength ||
                    text.IndexOf('\0') >= 0)
                {
                    throw new InvalidDataException(
                        "environment names and values must be bounded NUL-free strings");
                }
                if (result.Count >= MaxProtocolCollectionCount)
                {
                    throw new InvalidDataException("environment contains too many variables");
                }
                if (result.ContainsKey(entry.Key))
                {
                    throw new InvalidDataException("environment contains a case-insensitive duplicate name");
                }
                totalLength += entry.Key.Length + text.Length + 2;
                if (totalLength > MaxProtocolStringLength)
                {
                    throw new InvalidDataException("environment exceeds the protocol size bound");
                }
                result.Add(entry.Key, text);
            }
            return result;
        }

        private static string RequireAbsoluteExistingFile(string value, string label)
        {
            if (value.IndexOf('\0') >= 0 || !Path.IsPathRooted(value))
            {
                throw new InvalidDataException(label + " must be an absolute NUL-free path");
            }
            string fullPath = Path.GetFullPath(value);
            if (!File.Exists(fullPath))
            {
                throw new FileNotFoundException(label + " does not exist", fullPath);
            }
            return fullPath;
        }

        private static string RequireAbsoluteExistingDirectory(string value, string label)
        {
            if (value.IndexOf('\0') >= 0 || !Path.IsPathRooted(value))
            {
                throw new InvalidDataException(label + " must be an absolute NUL-free path");
            }
            string fullPath = Path.GetFullPath(value);
            if (!Directory.Exists(fullPath))
            {
                throw new DirectoryNotFoundException(label + " does not exist: " + fullPath);
            }
            return fullPath;
        }

        private static void EnsureHandle(IntPtr handle, string api)
        {
            if (handle == IntPtr.Zero || handle == new IntPtr(-1))
            {
                throw NativeFailure(api, Marshal.GetLastWin32Error());
            }
        }

        private static Exception NativeFailure(string api, int error)
        {
            return new Win32Exception(error, api + " failed");
        }

        private static void WriteError(string nonce, string stage, Exception error)
        {
            string message = error == null ? "unknown controller error" : error.Message;
            if (string.IsNullOrEmpty(message))
            {
                message = "unknown controller error";
            }
            message = message.Replace('\0', ' ');
            if (message.Length > MaxErrorMessageLength)
            {
                message = message.Substring(0, MaxErrorMessageLength);
            }
            WriteRecord(new Dictionary<string, object>
            {
                { "v", ProtocolVersion },
                { "type", "error" },
                { "nonce", nonce },
                { "stage", stage },
                { "message", message }
            });
        }

        private static void WriteRecord(Dictionary<string, object> record)
        {
            if (record == null)
            {
                return;
            }
            string line = Json.Serialize(record);
            lock (OutputLock)
            {
                Console.Out.WriteLine(line);
                Console.Out.Flush();
            }
        }
    }
}
