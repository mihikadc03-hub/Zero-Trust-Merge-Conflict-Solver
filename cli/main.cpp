
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>

struct Conflict {
    std::string before;
    std::string ours;
    std::string theirs;
    std::string after;
};

std::string read_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);

    if (!file) {
        throw std::runtime_error(
            "Could not open file: " + path.string()
        );
    }

    std::ostringstream buffer;
    buffer << file.rdbuf();
    return buffer.str();
}

std::optional<Conflict> parse_first_conflict(
    const std::string& content
) {
    const std::string start_marker = "<<<<<<< ";
    const std::string separator_marker = "=======";
    const std::string end_marker = ">>>>>>> ";

    std::size_t start = content.find(start_marker);

    if (start == std::string::npos ||
        (start != 0 && content[start - 1] != '\n')) {
        return std::nullopt;
    }

    std::size_t start_line_end = content.find('\n', start);
    if (start_line_end == std::string::npos) {
        return std::nullopt;
    }

    std::size_t separator = content.find(
        separator_marker, start_line_end + 1
    );

    if (separator == std::string::npos ||
        (separator != 0 && content[separator - 1] != '\n')) {
        return std::nullopt;
    }

    std::size_t separator_line_end =
        content.find('\n', separator);

    if (separator_line_end == std::string::npos) {
        return std::nullopt;
    }

    std::size_t end = content.find(
        end_marker, separator_line_end + 1
    );

    if (end == std::string::npos ||
        (end != 0 && content[end - 1] != '\n')) {
        return std::nullopt;
    }

    std::size_t end_line_end = content.find('\n', end);

    std::size_t after_start =
        end_line_end == std::string::npos
            ? content.size()
            : end_line_end + 1;

    Conflict conflict;

    conflict.before = content.substr(0, start);

    conflict.ours = content.substr(
        start_line_end + 1,
        separator - start_line_end - 2
    );

    conflict.theirs = content.substr(
        separator_line_end + 1,
        end - separator_line_end - 2
    );

    conflict.after = content.substr(after_start);

    return conflict;
}

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: resolver <file-path>\n";
        return 1;
    }

    try {
        std::string content = read_file(argv[1]);
        auto conflict = parse_first_conflict(content);

        if (!conflict) {
            std::cout << "No standard conflict found, "
                      << "or the conflict block is malformed.\n";
            return 1;
        }

        std::cout << "Code before conflict:\n"
                  << conflict->before;

        std::cout << "\nOURS:\n"
                  << conflict->ours;

        std::cout << "\nTHEIRS:\n"
                  << conflict->theirs;

        std::cout << "\nCode after conflict:\n"
                  << conflict->after;
    }
    catch (const std::exception& error) {
        std::cerr << "Error: " << error.what() << '\n';
        return 1;
    }

    return 0;
}
