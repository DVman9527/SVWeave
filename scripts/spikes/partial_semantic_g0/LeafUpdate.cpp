// Synthetic fixture-only native oracle. No production SVWeave API / pin changes.
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include "slang/ast/ASTSerializer.h"
#include "slang/ast/ASTVisitor.h"
#include "slang/ast/Compilation.h"
#include "slang/ast/symbols/CompilationUnitSymbols.h"
#include "slang/syntax/SyntaxTree.h"
#include "slang/text/Json.h"
#include "slang/text/SourceManager.h"
#ifndef PCU_STOCK
#include "slang/ast/ExperimentalLeafSession.h"
#endif
using namespace slang;
using namespace slang::ast;
static void require(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static std::string fixture(bool b, bool references = false) {
    if (references) {
        std::string s =
            "module changed(output bit y, k); bit a, b; assign y = a & a; assign k = a; endmodule\n"
            "module control(output bit y); bit a, b; assign y = a & a; endmodule\n"
            "module top(output bit x, x2, k, k2, z); changed u0(x,k); changed u2(x2,k2); control u1(z); endmodule\n";
        if (b) s.replace(s.find("a & a"), 5, "b & b");
        return s;
    }
    std::string s =
        "module changed(output bit y); assign y = 1'b0; endmodule\n"
        "module control(output bit y); assign y = 1'b1; endmodule\n"
        "module top(output bit x, x2, z); changed u0(x); changed u2(x2); control u1(z); endmodule\n";
    if (b) s[s.find("1'b0") + 3] = '1';
    return s;
}
static std::string quote(std::string_view s) {
    JsonWriter w; w.writeValue(s); return std::string(w.view());
}
static std::string envelope(std::string ast, bool b, bool references = false) {
    return "{\"ast\":" + ast + ",\"diagnostics\":[],\"active_source\":" + quote(fixture(b, references)) +
           ",\"logical_path\":\"fixture.sv\",\"macro_provenance\":[],"
           "\"scope\":" + quote(references ? "exact-three-module-local-bit-reference-fixture-only" : "exact-three-module-constant-rhs-fixture-only") + "}";
}
#ifndef PCU_STOCK
static std::string ledger(Compilation& comp) {
    JsonWriter writer;
    writer.startArray();
    auto visitor = makeVisitor([&](auto& visitor, const InstanceSymbol& instance) {
        for (auto& member : instance.body.members()) {
            if (auto value = member.as_if<VariableSymbol>(); value && value->getSyntax()) {
                auto it = comp.experimentalReferenceCounts.find(value->getSyntax());
                auto count = it == comp.experimentalReferenceCounts.end() ? std::pair<uint64_t,uint64_t>{0,0} : it->second;
                writer.startObject();
                writer.writeProperty("target"); writer.writeValue(value->getHierarchicalPath());
                writer.writeProperty("read_events"); writer.writeValue(count.first);
                writer.writeProperty("write_events"); writer.writeValue(count.second);
                writer.endObject();
            }
        }
        visitor.visitDefault(instance);
    });
    comp.getRoot().visit(visitor);
    writer.endArray();
    return std::string(writer.view());
}
#endif
static std::string full(bool b, bool references = false, std::string* eventLedger = nullptr) {
    SourceManager sources;
    auto tree = syntax::SyntaxTree::fromText(fixture(b, references), sources, "fixture.sv");
    Compilation comp;
    comp.addSyntaxTree(tree);
    // Diagnostics cannot be dropped / normalized: any diagnostic rejects this fixture.
    require(comp.getAllDiagnostics().empty(), "full build has diagnostics");
#ifndef PCU_STOCK
    auto diagnosticEmissions = comp.experimentalDiagnosticEmissions;
#endif
    JsonWriter w;
    ASTSerializer serializer(comp, w);
    serializer.setIncludeAddresses(false);
    serializer.setIncludeSourceInfo(true);
    serializer.setDetailedTypeInfo(true);
    w.startObject();
    w.writeProperty("root"); serializer.serialize(comp.getRoot());
    w.writeProperty("concrete_instances"); w.startArray();
    auto visitor = makeVisitor([&](auto& v, const InstanceSymbol& instance) {
        w.startObject();
        w.writeProperty("path"); w.writeValue(instance.getHierarchicalPath());
        w.writeProperty("body"); serializer.serialize(instance.body);
        w.writeProperty("resolved_targets"); w.startArray();
        auto targets = makeVisitor([&](auto&, const NamedValueExpression& expr) {
            w.writeValue(expr.symbol.getHierarchicalPath());
        });
        instance.body.visit(targets);
        w.endArray();
        w.writeProperty("usage_semantics");
        w.writeValue(std::string_view("per-instance-AST-occurrences; native-flags-shared-declaration-aggregate"));
        std::map<const ValueSymbol*, std::pair<uint64_t, uint64_t>> occurrenceCounts;
        for (auto& member : instance.body.members()) {
            if (auto assignment = member.as_if<ContinuousAssignSymbol>()) {
                auto& expr = assignment->getAssignment().as<AssignmentExpression>();
                auto reads = makeVisitor([&](auto&, const NamedValueExpression& ref) {
                    ++occurrenceCounts[&ref.symbol].first;
                });
                auto writes = makeVisitor([&](auto&, const NamedValueExpression& ref) {
                    ++occurrenceCounts[&ref.symbol].second;
                });
                expr.right().visit(reads); expr.left().visit(writes);
            }
        }
        w.writeProperty("reference_usage"); w.startArray();
        for (auto& member : instance.body.members()) {
            if (auto value = member.as_if<VariableSymbol>(); value && value->getSyntax()) {
                auto count = occurrenceCounts[value];
                auto flags = comp.isReferenced(*value->getSyntax());
                w.startObject();
                w.writeProperty("target"); w.writeValue(value->getHierarchicalPath());
                w.writeProperty("read_occurrences"); w.writeValue(count.first);
                w.writeProperty("write_occurrences"); w.writeValue(count.second);
                w.writeProperty("native_read"); w.writeValue(flags.first);
                w.writeProperty("native_write"); w.writeValue(flags.second);
                w.endObject();
            }
        }
        w.endArray();
        w.endObject(); v.visitDefault(instance);
    });
    comp.getRoot().visit(visitor);
    w.endArray(); w.endObject();
#ifndef PCU_STOCK
    require(comp.experimentalDiagnosticEmissions == diagnosticEmissions, "diagnostic during full serialization");
#endif
#ifndef PCU_STOCK
    if (eventLedger) *eventLedger = ledger(comp);
#else
    (void)eventLedger;
#endif
    return envelope(std::string(w.view()), b, references);
}
#ifndef PCU_STOCK
static std::string work(Compilation::ExperimentalWork w) {
    return "{\"expressions\":" + std::to_string(w.expressions) +
           ",\"lookups\":" + std::to_string(w.lookups) +
           ",\"assignments\":" + std::to_string(w.assignments) +
           ",\"conversions\":" + std::to_string(w.conversions) + "}";
}
static void incremental(bool references = false) {
    ExperimentalLeafSession session(references);
    require(ExperimentalLeafSession::fixture(false, references) == fixture(false, references) &&
            ExperimentalLeafSession::fixture(true, references) == fixture(true, references), "fixture drift");
    auto a0 = envelope(session.snapshot(0), false, references);
    require(a0 == full(false, references), "initial full oracle mismatch");
    auto control0 = session.controlWork();
    auto changed0 = session.changedWork();
    require(control0.expressions > 0 && control0.lookups > 0 &&
            control0.assignments == 1 && control0.conversions > 0, "missing native baseline work");
    std::vector<std::string> rejected;
    auto edit = [&](std::string from, std::string to) {
        auto s = fixture(false, references); s.replace(s.find(from), from.size(), to); rejected.push_back(s);
    };
    std::string original = references ? "a & a" : "1'b0";
    edit(original, "missing"); // negative lookup, rejected BEFORE semantic mutation
    edit(original, "a"); // reference-changing update deliberately unsupported
    edit(original, "2'b00");
    edit("output bit y", "output logic [3:0] y");
    edit("assign y", "bit extra; assign y");
    edit(references ? "changed u0(x,k);" : "changed u0(x);", "");
    edit("module changed", "module renamed");
    edit(original, "(");
    for (auto& s : rejected) {
        require(!session.update(s), "unsupported edit admitted");
        require(session.generation() == 0 && envelope(session.snapshot(0), false, references) == a0,
                "rejected edit changed prior snapshot or diagnostics");
        require(session.changedWork() == changed0 && session.controlWork() == control0,
                "rejected edit performed native semantic work");
    }
    require(session.update(fixture(false, references)), "same-byte no-op denied");
    require(session.generation() == 0 && session.changedWork() == changed0 &&
            session.controlWork() == control0 && envelope(session.snapshot(0), false, references) == a0,
            "no-op did work or changed state");
    auto ledger0 = session.referenceLedger(0);
    std::string fullLedger0;
    (void)full(false, references, &fullLedger0);
    require(ledger0 == fullLedger0, "initial native event ledger mismatch");
    if (references) require(session.referenceUsageVerified(), "baseline reference usage incorrect");
    require(session.update(fixture(true, references)), "A to B denied");
    auto b1 = envelope(session.snapshot(1), true, references);
    require(b1 == full(true, references), "B full oracle mismatch");
    auto ledger1 = session.referenceLedger(1);
    std::string fullLedger1;
    (void)full(true, references, &fullLedger1);
    require(ledger1 == fullLedger1, "B native event ledger mismatch");
    if (references) require(session.referenceUsageVerified(), "shared reference contribution lost");
    auto changed1 = session.changedWork();
    if (references) require(changed1.lookups > changed0.lookups, "no actual name lookup rebinding");
    require(session.update(fixture(true, references)) && session.generation() == 1 &&
            session.changedWork() == changed1 && session.referenceLedger(1) == ledger1,
            "B no-op changed generation or reference effects");
    for (auto& rejectedBytes : rejected) {
        require(!session.update(rejectedBytes), "invalid edit accepted after B");
        require(envelope(session.snapshot(1), true, references) == b1 &&
                session.referenceLedger(1) == ledger1 && session.changedWork() == changed1,
                "rejection damaged accepted B result");
    }
    require(changed1.expressions > changed0.expressions && changed1.conversions > changed0.conversions,
            "no native changed-leaf rebinding work");
    require(session.controlRetained() && session.unaffectedWorkRetained() && session.controlWork() == control0,
            "control rebound or lost identity");
    bool stale = false;
    try { (void)session.snapshot(0); } catch (const std::runtime_error&) { stale = true; }
    require(stale, "stale handle accepted");
    require(session.update(fixture(false, references)), "B to A denied");
    auto a2 = envelope(session.snapshot(2), false, references);
    require(a2 == full(false, references) && a2 == a0, "revert oracle mismatch");
    require(session.controlRetained() && session.unaffectedWorkRetained() && session.controlWork() == control0, "control rebound on revert");
    require(session.update(fixture(false, references)) && session.generation() == 2,
            "same-byte no-op rejected at generation bound");
    require(!session.update(fixture(true, references)), "generation bound bypassed");
    require(envelope(session.snapshot(2), false, references) == a2, "bound rejection changed session");
    auto ledger2 = session.referenceLedger(2);
    std::string fullLedger2;
    (void)full(false, references, &fullLedger2);
    require(ledger2 == fullLedger2 && ledger2 == ledger0, "reverted native event ledger mismatch");
    if (references) require(session.referenceUsageVerified(), "last b reference was not withdrawn");
    require(session.unaffectedAssignmentsRetained(), "unaffected assignment identity changed");
    auto changed2 = session.changedWork();
    if (references) require(changed2.lookups > changed1.lookups, "revert omitted native name lookup");
    require(changed2.expressions > changed1.expressions && changed2.conversions > changed1.conversions,
            "revert did not rebind");
    std::cout << "{\"status\":" << quote(references ? "REFERENCE_MICRO_PROTOTYPE_PASS" : "MICRO_PROTOTYPE_PASS") << ",\"general_g0\":\"NOT_ESTABLISHED\","
        "\"generations\":[" << a0 << ',' << b1 << ',' << a2 << "],"
        "\"assertions\":{\"full_equivalence\":true,\"stale_handle_rejected\":true,"
        "\"unsupported_edits_rejected\":true,\"rejection_atomic\":true,"
        "\"control_identity_retained\":true,\"control_native_work_unchanged\":true,"
        "\"all_changed_occurrences_updated\":true,\"source_provenance_verified\":true,\"unaffected_native_work_unchanged\":true,\"changed_native_work_observed\":true,\"generation_limit_enforced\":true"
        << (references ? ",\"same_bytes_noop\":true,\"native_lookup_rebound\":true,\"shared_contributions_preserved\":true,\"last_reference_withdrawn\":true,\"reference_event_ledger_equivalent\":true" : "") << "},"
        "\"counters\":{\"control_before\":" << work(control0) << ",\"control_after\":"
        << work(session.controlWork()) << ",\"changed_before\":" << work(changed0)
        << ",\"changed_after_b\":" << work(changed1) << ",\"changed_after_a\":" << work(changed2)
        << "}";
    if (references) std::cout << ",\"reference_event_ledgers\":[" << ledger0 << "," << ledger1 << "," << ledger2 << "]";
    std::cout << "}\n";
}
#endif
int main(int argc, char** argv) {
    try {
        if (argc == 3 && (std::string(argv[1]) == "--full" || std::string(argv[1]) == "--reference-full") &&
            (std::string(argv[2]) == "a" || std::string(argv[2]) == "b")) {
            std::cout << full(std::string(argv[2]) == "b", std::string(argv[1]) == "--reference-full") << '\n'; return 0;
        }
#ifndef PCU_STOCK
        if (argc == 3 && std::string(argv[1]) == "--reference-ledger-full" &&
            (std::string(argv[2]) == "a" || std::string(argv[2]) == "b")) {
            std::string events; (void)full(std::string(argv[2]) == "b", true, &events);
            std::cout << events << '\n'; return 0;
        }
        if (argc == 2 && std::string(argv[1]) == "--reference-incremental") { incremental(true); return 0; }
        if (argc == 2 && std::string(argv[1]) == "--incremental") { incremental(); return 0; }
#endif
        throw std::runtime_error("usage: --full a|b or fork-only --incremental");
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
