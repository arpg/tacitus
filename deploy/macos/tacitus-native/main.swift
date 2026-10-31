// tacitus-native: the macOS helper of Tacitus's native-accounts backend
// (docs/macos-native-accounts.md). EventKit and Contacts run in this
// process; Mail.app is driven by mail.js under /usr/bin/osascript. JSON in,
// JSON out (the protocol in preprocessor/native_accounts.py).
//
//   tacitus-native call               one request on stdin, one answer on stdout
//   tacitus-native serve --queue DIR  answer req-<id>.json files in DIR (0700)
//   tacitus-native version
//
// The launch agent runs `serve` in the owner's login session, so the
// permission prompts and grants (Calendars, Contacts, Automation of Mail)
// belong to this binary. Build: build.sh beside this file (swiftc, no Xcode
// project). Status: written on Linux against Apple's documentation, not yet
// compiled or run on a Mac.

import Contacts
import CoreServices
import EventKit
import Foundation

let protocolVersion = 1
let helperVersion = "0.1.0"
let maxRequestBytes = 4 * 1024 * 1024

typealias JSONObject = [String: Any]

struct HelperError: Error {
    let code: String
    let message: String
}

final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}

// MARK: - Dates

let isoOut: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone.current
    return f
}()

let isoIn: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

let isoInFraction: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

func parseDate(_ value: Any?, _ name: String) throws -> Date {
    guard let text = value as? String else {
        throw HelperError(code: "bad-request", message: "\(name) is missing")
    }
    if let date = isoIn.date(from: text) ?? isoInFraction.date(from: text) {
        return date
    }
    throw HelperError(code: "bad-request", message: "\(name) is not an ISO 8601 time with an offset")
}

func string(_ args: JSONObject, _ key: String) -> String? {
    return args[key] as? String
}

// MARK: - Permissions

let store = EKEventStore()

func calendarStatus() -> String {
    switch EKEventStore.authorizationStatus(for: .event) {
    case .fullAccess: return "authorized"
    case .writeOnly: return "write-only"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not-determined"
    default: return "unknown"
    }
}

func contactsStatus() -> String {
    switch CNContactStore.authorizationStatus(for: .contacts) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not-determined"
    default: return "unknown"
    }
}

// Asks the system whether this binary may send Apple events to Mail, without
// a prompt unless `ask`. -1743 errAEEventNotPermitted, -1744
// errAEEventWouldRequireUserConsent, -600 procNotFound (Mail not running).
func mailStatus(ask: Bool) -> String {
    let target = NSAppleEventDescriptor(bundleIdentifier: "com.apple.mail")
    guard let desc = target.aeDesc else { return "unknown" }
    let status = AEDeterminePermissionToAutomateTarget(desc, typeWildCard, typeWildCard, ask)
    switch Int(status) {
    case 0: return "authorized"
    case -1743: return "denied"
    case -1744: return "not-determined"
    case -600: return "mail-not-running"
    default: return "error \(status)"
    }
}

func requestCalendarAccess() -> Bool {
    let done = DispatchSemaphore(value: 0)
    let granted = Box(false)
    store.requestFullAccessToEvents { ok, _ in
        granted.value = ok
        done.signal()
    }
    done.wait()
    return granted.value
}

func requestContactsAccess() -> Bool {
    let done = DispatchSemaphore(value: 0)
    let granted = Box(false)
    CNContactStore().requestAccess(for: .contacts) { ok, _ in
        granted.value = ok
        done.signal()
    }
    done.wait()
    return granted.value
}

func requireCalendar() throws {
    let state = calendarStatus()
    if state == "authorized" { return }
    throw HelperError(code: state == "not-determined" ? "not-determined" : "permission-denied",
                      message: "calendar access is \(state)")
}

// MARK: - Calendars

func sourceTypeName(_ type: EKSourceType?) -> String {
    guard let type = type else { return "unknown" }
    switch type {
    case .local: return "local"
    case .exchange: return "exchange"
    case .calDAV: return "caldav"
    case .mobileMe: return "mobileme"
    case .subscribed: return "subscribed"
    case .birthdays: return "birthdays"
    default: return "unknown"
    }
}

func availabilityName(_ value: EKEventAvailability) -> String {
    switch value {
    case .busy: return "busy"
    case .free: return "free"
    case .tentative: return "tentative"
    case .unavailable: return "unavailable"
    default: return "not-supported"
    }
}

func availabilityValue(_ name: String, for calendar: EKCalendar) -> EKEventAvailability? {
    let mask = calendar.supportedEventAvailabilities
    switch name {
    case "busy": return mask.contains(.busy) ? .busy : nil
    case "free": return mask.contains(.free) ? .free : nil
    case "tentative": return mask.contains(.tentative) ? .tentative : nil
    case "unavailable": return mask.contains(.unavailable) ? .unavailable : nil
    default: return nil
    }
}

func statusName(_ value: EKEventStatus) -> String {
    switch value {
    case .confirmed: return "confirmed"
    case .tentative: return "tentative"
    case .canceled: return "canceled"
    default: return "none"
    }
}

func participantStatusName(_ value: EKParticipantStatus) -> String {
    switch value {
    case .pending: return "pending"
    case .accepted: return "accepted"
    case .declined: return "declined"
    case .tentative: return "tentative"
    case .delegated: return "delegated"
    case .completed: return "completed"
    case .inProcess: return "in-process"
    default: return "unknown"
    }
}

func participantRoleName(_ value: EKParticipantRole) -> String {
    switch value {
    case .required: return "required"
    case .optional: return "optional"
    case .chair: return "chair"
    case .nonParticipant: return "non-participant"
    default: return "unknown"
    }
}

func participantTypeName(_ value: EKParticipantType) -> String {
    switch value {
    case .person: return "person"
    case .room: return "room"
    case .resource: return "resource"
    case .group: return "group"
    default: return "unknown"
    }
}

// EventKit reads attendees but cannot add them; a participant's address is
// its mailto: URL.
func participantJSON(_ p: EKParticipant) -> JSONObject {
    var email = ""
    let url = p.url.absoluteString
    if url.lowercased().hasPrefix("mailto:") {
        email = String(url.dropFirst(7)).removingPercentEncoding ?? String(url.dropFirst(7))
    }
    return [
        "name": p.name ?? "",
        "email": email,
        "status": participantStatusName(p.participantStatus),
        "role": participantRoleName(p.participantRole),
        "type": participantTypeName(p.participantType),
        "is_current_user": p.isCurrentUser,
    ]
}

func eventJSON(_ e: EKEvent) -> JSONObject {
    var out: JSONObject = [
        "id": e.eventIdentifier ?? "",
        "external_id": e.calendarItemExternalIdentifier ?? "",
        "calendar_id": e.calendar?.calendarIdentifier ?? "",
        "calendar_title": e.calendar?.title ?? "",
        "source_type": sourceTypeName(e.calendar?.source?.sourceType),
        "title": e.title ?? "",
        "start": isoOut.string(from: e.startDate),
        "end": isoOut.string(from: e.endDate),
        "all_day": e.isAllDay,
        "availability": availabilityName(e.availability),
        "status": statusName(e.status),
        "location": e.location ?? "",
        "notes": String((e.notes ?? "").prefix(4000)),
        "recurring": e.hasRecurrenceRules,
        "detached": e.isDetached,
        "attendees": (e.attendees ?? []).map(participantJSON),
    ]
    if let occurrence = e.occurrenceDate {
        out["occurrence"] = isoOut.string(from: occurrence)
    }
    if let organizer = e.organizer {
        out["organizer"] = participantJSON(organizer)
    }
    return out
}

func listCalendars() throws -> JSONObject {
    try requireCalendar()
    let calendars = store.calendars(for: .event).map { c -> JSONObject in
        [
            "id": c.calendarIdentifier,
            "title": c.title,
            "source": c.source?.title ?? "",
            "source_type": sourceTypeName(c.source?.sourceType),
            "writable": c.allowsContentModifications,
        ]
    }
    return ["calendars": calendars]
}

func namedCalendars(_ args: JSONObject) throws -> [EKCalendar] {
    guard let ids = args["calendars"] as? [String], !ids.isEmpty else {
        throw HelperError(code: "bad-request", message: "list-events names its calendars")
    }
    let found = ids.compactMap { store.calendar(withIdentifier: $0) }
    if found.isEmpty {
        throw HelperError(code: "not-found", message: "none of the calendars named exists")
    }
    return found
}

func listEvents(_ args: JSONObject) throws -> JSONObject {
    try requireCalendar()
    let calendars = try namedCalendars(args)
    let start = try parseDate(args["start"], "start")
    let end = try parseDate(args["end"], "end")
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: calendars)
    let events = store.events(matching: predicate)
        .sorted { ($0.startDate ?? .distantPast) < ($1.startDate ?? .distantPast) }
    return ["events": events.map(eventJSON)]
}

// One event by its identifier; a recurring event's occurrence also by its
// occurrence date, since every occurrence shares the identifier.
func findEvent(_ args: JSONObject) throws -> EKEvent {
    guard let id = string(args, "id"), !id.isEmpty else {
        throw HelperError(code: "bad-request", message: "an event id is missing")
    }
    guard let first = store.event(withIdentifier: id) else {
        throw HelperError(code: "not-found", message: "no event has that id")
    }
    guard let occurrenceText = string(args, "occurrence"), !occurrenceText.isEmpty else {
        return first
    }
    let occurrence = try parseDate(occurrenceText, "occurrence")
    let predicate = store.predicateForEvents(withStart: occurrence.addingTimeInterval(-86400),
                                             end: occurrence.addingTimeInterval(86400),
                                             calendars: [first.calendar])
    for e in store.events(matching: predicate) where e.eventIdentifier == id {
        if let o = e.occurrenceDate, abs(o.timeIntervalSince(occurrence)) < 1 {
            return e
        }
    }
    throw HelperError(code: "not-found", message: "no occurrence of that event at that time")
}

func span(_ args: JSONObject) -> EKSpan {
    return string(args, "span") == "future" ? .futureEvents : .thisEvent
}

func writableCalendar(_ id: String?) throws -> EKCalendar {
    guard let id = id, let calendar = store.calendar(withIdentifier: id) else {
        throw HelperError(code: "not-found", message: "no calendar has that id")
    }
    guard calendar.allowsContentModifications else {
        throw HelperError(code: "permission-denied", message: "the calendar does not allow changes")
    }
    return calendar
}

func apply(_ args: JSONObject, to event: EKEvent) throws {
    if let title = string(args, "title") { event.title = title }
    if args["start"] != nil { event.startDate = try parseDate(args["start"], "start") }
    if args["end"] != nil { event.endDate = try parseDate(args["end"], "end") }
    if let allDay = args["all_day"] as? Bool { event.isAllDay = allDay }
    if let location = string(args, "location") { event.location = location }
    if let notes = string(args, "notes") { event.notes = notes }
    if let name = string(args, "availability"), let calendar = event.calendar,
       let value = availabilityValue(name, for: calendar) {
        event.availability = value
    }
}

func createEvent(_ args: JSONObject) throws -> JSONObject {
    try requireCalendar()
    if let attendees = args["attendees"] as? [Any], !attendees.isEmpty {
        throw HelperError(code: "unsupported", message: "EventKit cannot add attendees")
    }
    let calendar = try writableCalendar(string(args, "calendar_id"))
    let event = EKEvent(eventStore: store)
    event.calendar = calendar
    try apply(args, to: event)
    try store.save(event, span: .thisEvent, commit: true)
    return ["event": eventJSON(event)]
}

func updateEvent(_ args: JSONObject) throws -> JSONObject {
    try requireCalendar()
    let event = try findEvent(args)
    _ = try writableCalendar(event.calendar?.calendarIdentifier)
    try apply(args, to: event)
    try store.save(event, span: span(args), commit: true)
    return ["event": eventJSON(event)]
}

func deleteEvent(_ args: JSONObject) throws -> JSONObject {
    try requireCalendar()
    let event = try findEvent(args)
    _ = try writableCalendar(event.calendar?.calendarIdentifier)
    try store.remove(event, span: span(args), commit: true)
    return ["deleted": true]
}

// MARK: - Contacts

func findContacts(_ args: JSONObject) throws -> JSONObject {
    let state = contactsStatus()
    guard state == "authorized" else {
        throw HelperError(code: state == "not-determined" ? "not-determined" : "permission-denied",
                          message: "contacts access is \(state)")
    }
    let predicate: NSPredicate
    if let email = string(args, "email"), !email.isEmpty {
        predicate = CNContact.predicateForContacts(matchingEmailAddress: email)
    } else if let name = string(args, "name"), !name.isEmpty {
        predicate = CNContact.predicateForContacts(matchingName: name)
    } else {
        throw HelperError(code: "bad-request", message: "find-contacts takes a name or an email")
    }
    let keys: [CNKeyDescriptor] = [
        CNContactGivenNameKey as CNKeyDescriptor,
        CNContactFamilyNameKey as CNKeyDescriptor,
        CNContactOrganizationNameKey as CNKeyDescriptor,
        CNContactEmailAddressesKey as CNKeyDescriptor,
    ]
    let found = try CNContactStore().unifiedContacts(matching: predicate, keysToFetch: keys)
    let top = (args["top"] as? Int) ?? 10
    let contacts = found.prefix(top).map { c -> JSONObject in
        [
            "name": [c.givenName, c.familyName].filter { !$0.isEmpty }.joined(separator: " "),
            "organization": c.organizationName,
            "emails": c.emailAddresses.map { $0.value as String },
        ]
    }
    return ["contacts": Array(contacts)]
}

// MARK: - Mail, through mail.js

let mailVerbs: Set<String> = [
    "mail-status", "list-mailboxes", "list-messages", "search-messages", "get-message",
    "find-thread", "save-draft", "delete-draft", "send-message", "move-message", "set-flags",
]

// mail.js beside this binary, and only when no other user can change it or
// the directory it is in: the helper runs it with the owner's Automation
// grant.
func mailScript() throws -> URL {
    let exe = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0]))
        .resolvingSymlinksInPath()
    let script = exe.deletingLastPathComponent().appendingPathComponent("mail.js")
    for path in [script.path, script.deletingLastPathComponent().path] {
        let attrs = try FileManager.default.attributesOfItem(atPath: path)
        let owner = (attrs[.ownerAccountID] as? NSNumber)?.uint32Value ?? UInt32.max
        let mode = (attrs[.posixPermissions] as? NSNumber)?.intValue ?? 0o777
        if (owner != 0 && owner != getuid()) || mode & 0o022 != 0 {
            throw HelperError(code: "internal", message: "\(path) is writable by another user")
        }
    }
    return script
}

func runMail(verb: String, args: JSONObject) throws -> JSONObject {
    let script = try mailScript()
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    process.arguments = ["-l", "JavaScript", script.path]
    let input = Pipe(), output = Pipe(), errors = Pipe()
    process.standardInput = input
    process.standardOutput = output
    process.standardError = errors
    try process.run()
    let request = try JSONSerialization.data(withJSONObject: ["verb": verb, "args": args])
    try? input.fileHandleForWriting.write(contentsOf: request)
    try? input.fileHandleForWriting.close()
    // A send or a search of a large mailbox takes longer than a read.
    let seconds: Double = ["send-message", "search-messages", "find-thread"].contains(verb) ? 170 : 90
    let timer = DispatchWorkItem { if process.isRunning { process.terminate() } }
    DispatchQueue.global().asyncAfter(deadline: .now() + seconds, execute: timer)
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    timer.cancel()
    guard let answer = (try? JSONSerialization.jsonObject(with: data)) as? JSONObject else {
        let tail = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        throw HelperError(code: process.terminationReason == .uncaughtSignal ? "timeout" : "internal",
                          message: "mail.js gave no answer: \(tail.suffix(300))")
    }
    if answer["ok"] as? Bool == true, let result = answer["result"] as? JSONObject {
        return result
    }
    let error = answer["error"] as? JSONObject ?? [:]
    throw HelperError(code: error["code"] as? String ?? "internal",
                      message: error["message"] as? String ?? "mail.js failed")
}

// MARK: - Status and first run

func status() -> JSONObject {
    let mail = mailStatus(ask: false)
    return [
        "version": helperVersion,
        "protocol": protocolVersion,
        "permissions": [
            "calendar": calendarStatus(),
            "contacts": contactsStatus(),
            "mail": mail,
        ],
        "mail_running": mail != "mail-not-running",
    ]
}

func requestAccess(_ args: JSONObject) -> JSONObject {
    if args["calendar"] as? Bool == true && calendarStatus() == "not-determined" {
        _ = requestCalendarAccess()
    }
    if args["contacts"] as? Bool == true && contactsStatus() == "not-determined" {
        _ = requestContactsAccess()
    }
    if args["mail"] as? Bool == true {
        _ = mailStatus(ask: true)
    }
    return status()
}

// MARK: - Dispatch

func handle(_ request: JSONObject) -> JSONObject {
    var response: JSONObject = ["v": protocolVersion, "id": request["id"] as? String ?? ""]
    do {
        guard request["v"] as? Int == protocolVersion else {
            throw HelperError(code: "protocol", message: "protocol version mismatch")
        }
        guard let verb = request["verb"] as? String else {
            throw HelperError(code: "bad-request", message: "the request names no verb")
        }
        let args = request["args"] as? JSONObject ?? [:]
        let result: JSONObject
        switch verb {
        case "status": result = status()
        case "request-access": result = requestAccess(args)
        case "list-calendars": result = try listCalendars()
        case "list-events": result = try listEvents(args)
        case "get-event":
            try requireCalendar()
            result = ["event": eventJSON(try findEvent(args))]
        case "create-event": result = try createEvent(args)
        case "update-event": result = try updateEvent(args)
        case "delete-event": result = try deleteEvent(args)
        case "find-contacts": result = try findContacts(args)
        default:
            guard mailVerbs.contains(verb) else {
                throw HelperError(code: "unknown-verb", message: "unknown verb \(verb)")
            }
            result = try runMail(verb: verb, args: args)
        }
        response["ok"] = true
        response["result"] = result
    } catch let error as HelperError {
        response["ok"] = false
        response["error"] = ["code": error.code, "message": error.message]
    } catch {
        response["ok"] = false
        response["error"] = ["code": "internal", "message": String(describing: error)]
    }
    return response
}

func answer(_ data: Data) -> Data {
    var response: JSONObject
    if data.count > maxRequestBytes {
        response = ["v": protocolVersion, "id": "", "ok": false,
                    "error": ["code": "bad-request", "message": "the request is too large"]]
    } else if let request = (try? JSONSerialization.jsonObject(with: data)) as? JSONObject {
        response = handle(request)
    } else {
        response = ["v": protocolVersion, "id": "", "ok": false,
                    "error": ["code": "bad-request", "message": "the request is not a JSON object"]]
    }
    return (try? JSONSerialization.data(withJSONObject: response, options: [.sortedKeys]))
        ?? Data("{\"ok\":false}".utf8)
}

// MARK: - Serving the queue

// The queue is the owner-private directory the Python side writes: a real
// directory of this user that no one else may read, write or enter.
func queueIsSafe(_ path: String) -> Bool {
    var info = stat()
    guard lstat(path, &info) == 0 else { return false }
    return (info.st_mode & S_IFMT) == S_IFDIR && info.st_uid == getuid() && (info.st_mode & 0o077) == 0
}

func serve(queue: String) -> Int32 {
    umask(0o077)
    guard queueIsSafe(queue) else {
        FileHandle.standardError.write(Data("tacitus-native: \(queue) must be this user's directory, mode 0700\n".utf8))
        return 78
    }
    let fm = FileManager.default
    let idPattern = try! NSRegularExpression(pattern: "^req-([0-9a-f]{32})\\.json$")
    while true {
        autoreleasepool {
            let names = ((try? fm.contentsOfDirectory(atPath: queue)) ?? []).sorted()
            for name in names {
                let range = NSRange(name.startIndex..., in: name)
                guard let match = idPattern.firstMatch(in: name, range: range),
                      let idRange = Range(match.range(at: 1), in: name) else { continue }
                let id = String(name[idRange])
                let work = "\(queue)/.\(id).work"
                // Claim the request: a rename is atomic, so one server takes it.
                guard rename("\(queue)/\(name)", work) == 0 else { continue }
                let data = fm.contents(atPath: work) ?? Data()
                unlink(work)
                let reply = answer(data)
                let target = URL(fileURLWithPath: "\(queue)/resp-\(id).json")
                try? reply.write(to: target, options: .atomic)
            }
        }
        // The run loop delivers EventKit's change notifications between polls.
        RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.2))
    }
}

// MARK: - Main

// A child that exits before reading its input must not end this process.
signal(SIGPIPE, SIG_IGN)
let arguments = CommandLine.arguments
switch arguments.count > 1 ? arguments[1] : "" {
case "call":
    let data = FileHandle.standardInput.readDataToEndOfFile()
    FileHandle.standardOutput.write(answer(data))
    exit(0)
case "serve":
    guard arguments.count == 4, arguments[2] == "--queue" else {
        FileHandle.standardError.write(Data("usage: tacitus-native serve --queue DIR\n".utf8))
        exit(64)
    }
    exit(serve(queue: arguments[3]))
case "version":
    print("{\"version\": \"\(helperVersion)\", \"protocol\": \(protocolVersion)}")
    exit(0)
default:
    FileHandle.standardError.write(Data("usage: tacitus-native call | serve --queue DIR | version\n".utf8))
    exit(64)
}
